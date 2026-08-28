/// <reference types="node" />

import * as http from 'http';
import * as vscode from 'vscode';
declare const require: any;
import * as cp from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import { randomBytes } from 'crypto';

// --- SERENITY SAFE HTTP SHIM (Replaces axios) ---
function serenityHttp<T = any>(method: string, url: string, body?: unknown, timeoutMs = 5000, signal?: AbortSignal): Promise<{ status: number; data: T }> {
    return new Promise((resolve, reject) => {
        let settled = false;
        const u = new URL(url);
        const payload = body === undefined ? undefined : JSON.stringify(body);
        const req = http.request({
            hostname: u.hostname,
            port: Number(u.port || 80),
            path: `${u.pathname}${u.search}`,
            method,
            headers: {
                'Content-Type': 'application/json',
                ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {})
            }
        }, (res) => {
            const chunks: Buffer[] = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => {
                if (settled) { return; }
                settled = true;
                const raw = Buffer.concat(chunks).toString('utf8');
                let data: any = raw;
                try { if (raw.length > 0) { data = JSON.parse(raw); } } catch { /* ignore parse errors */ }
                resolve({ status: res.statusCode || 0, data });
            });
        });
        const timer = setTimeout(() => {
            if (!settled) {
                settled = true;
                const error = new Error(`SerenityDev HTTP timeout after ${timeoutMs}ms`);
                req.destroy(error);
                reject(error);
            }
        }, timeoutMs);
        const onAbort = () => {
            if (!settled) {
                settled = true;
                const error = new Error('SerenityDev request aborted');
                req.destroy(error);
                reject(error);
            }
        };
        if (signal) {
            if (signal.aborted) {
                onAbort();
            } else {
                signal.addEventListener('abort', onAbort, { once: true });
            }
        }
        req.on('error', (err) => {
            if (!settled) { settled = true; clearTimeout(timer); reject(err); }
        });
        req.on('close', () => {
            clearTimeout(timer);
            if (signal) { signal.removeEventListener('abort', onAbort); }
        });
        if (payload) {
            req.write(payload);
        }
        req.end();
    });
}

function makeHttpError(status: number, data: any): any {
    const e: any = new Error(`HTTP ${status}`);
    e.response = { status, data };
    return e;
}

type SerenityHttpError = Error & { response?: { status: number; data: any } };

interface SerenityAxiosShim {
    isAxiosError(error: unknown): error is SerenityHttpError;
    get<T = any>(url: string, opts?: any): Promise<{ status: number; data: T }>;
    post<T = any>(url: string, body?: any, opts?: any): Promise<{ status: number; data: T }>;
    delete<T = any>(url: string, opts?: any): Promise<{ status: number; data: T }>;
}

async function serenityAxiosRequest<T = any>(method: 'GET' | 'POST' | 'DELETE', url: string, body?: unknown, opts?: { timeout?: number; signal?: AbortSignal }): Promise<{ status: number; data: T }> {
    const timeout = opts?.timeout || 5000;
    const r = await serenityHttp(method, url, body, timeout, opts?.signal);
    if (r.status >= 400) {
        throw makeHttpError(r.status, r.data);
    }
    return r;
}
// Shim to maintain compatibility with existing code using `axios`
const axios: SerenityAxiosShim = {
    isAxiosError: (error: unknown): error is SerenityHttpError => !!error && typeof error === 'object' && 'response' in error,
    get: <T = any>(url: string, opts?: any) => serenityAxiosRequest<T>('GET', url, undefined, opts),
    post: <T = any>(url: string, body?: any, opts?: any) => serenityAxiosRequest<T>('POST', url, body, opts),
    delete: <T = any>(url: string, opts?: any) => serenityAxiosRequest<T>('DELETE', url, undefined, opts)
};
// Global safety net to prevent extension host crashes from unhandled rejections
const unhandledRejectionMarker = '__serenityDevUnhandledRejectionHandler';
const processWithMarker = process as NodeJS.Process & { [unhandledRejectionMarker]?: boolean };
if (!processWithMarker[unhandledRejectionMarker]) {
    processWithMarker[unhandledRejectionMarker] = true;
    process.on('unhandledRejection', (reason) => {
        console.error('[SerenityDev] Unhandled Rejection:', reason);
    });
}

let serverProcess: cp.ChildProcess | undefined;
let serverOutputChannel: vscode.OutputChannel;
let statusBarItem: vscode.StatusBarItem;

const SERVER_HOST = '127.0.0.1';
const SERVER_PORT = 8002;
const SERVER_BASE = `http://${SERVER_HOST}:${SERVER_PORT}`;
const API_BASE = `${SERVER_BASE}/api`;
const ASK_URL = `${SERVER_BASE}/ask`;

function createSafeMarkdown(content: string): vscode.MarkdownString {
    const md = new vscode.MarkdownString(content);
    md.isTrusted = true;
    md.supportHtml = true;
    return md;
}

function getErrorMessage(error: unknown): string {
    if (axios.isAxiosError(error)) {
        const detail = error.response?.data?.detail;
        if (typeof detail === 'string' && detail) { return detail; }
        const message = error.response?.data?.message;
        if (typeof message === 'string' && message) { return message; }
        return error.message;
    }
    if (error instanceof Error) { return error.message; }
    if (typeof error === 'string') { return error; }
    if (error && typeof error === 'object') {
        try { return JSON.stringify(error); } catch { return 'Unknown error'; }
    }
    return 'Unknown error';
}

function parseSseLine(line: string, onEvent: (data: any) => void): void {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data:')) { return; }
    const payload = trimmed.substring(5).trim();
    if (!payload || payload === '[DONE]') { return; }
    try {
        onEvent(JSON.parse(payload));
    } catch (error) {
        console.warn('SerenityDev returned invalid SSE data:', payload, error);
    }
}

function findPythonInterpreter(context: vscode.ExtensionContext): { pythonPath: string; venvDir?: string } {
    const isWin = process.platform === 'win32';
    const config = vscode.workspace.getConfiguration('serenitydev');
    let customPy = config.get<string>('pythonPath');

    // Fall back to standard VS Code python.defaultInterpreterPath if serenitydev.pythonPath not customized
    if (!customPy || customPy === 'python') {
        const pyConfig = vscode.workspace.getConfiguration('python');
        const defaultPy = pyConfig.get<string>('defaultInterpreterPath');
        if (defaultPy && defaultPy !== 'python') {
            customPy = defaultPy;
        }
    }

    if (customPy && customPy !== 'python') {
        // Resolve ${workspaceFolder} variable if present
        if (customPy.includes('${workspaceFolder}') && vscode.workspace.workspaceFolders?.[0]) {
            customPy = customPy.replace(/\$\{workspaceFolder\}/g, vscode.workspace.workspaceFolders[0].uri.fsPath);
        }
        if (fs.existsSync(customPy)) {
            const stat = fs.statSync(customPy);
            if (stat.isFile()) {
                // Determine potential venv root from interpreter path (e.g. .venv/Scripts/python.exe -> .venv)
                const binDir = path.dirname(customPy);
                const parentDir = path.dirname(binDir);
                const isVenv = fs.existsSync(path.join(parentDir, 'pyvenv.cfg')) ||
                    fs.existsSync(path.join(binDir, 'activate')) ||
                    fs.existsSync(path.join(binDir, 'activate.bat')) ||
                    fs.existsSync(path.join(binDir, 'Activate.ps1'));
                return { pythonPath: customPy, venvDir: isVenv ? parentDir : undefined };
            } else if (stat.isDirectory()) {
                const candPy = isWin ? path.join(customPy, 'Scripts', 'python.exe') : path.join(customPy, 'bin', 'python');
                if (fs.existsSync(candPy)) {
                    return { pythonPath: candPy, venvDir: customPy };
                }
            }
        }
    }

    const candidateDirs: string[] = [];

    if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
        for (const wf of vscode.workspace.workspaceFolders) {
            candidateDirs.push(path.join(wf.uri.fsPath, '.venv'));
            candidateDirs.push(path.join(wf.uri.fsPath, 'venv'));
        }
    }

    candidateDirs.push(
        path.join(context.extensionPath, '.venv'),
        path.join(context.extensionPath, 'venv')
    );

    for (const cand of candidateDirs) {
        if (!fs.existsSync(cand)) { continue; }
        const candPy = isWin ? path.join(cand, 'Scripts', 'python.exe') : path.join(cand, 'bin', 'python');
        if (fs.existsSync(candPy)) {
            return { pythonPath: candPy, venvDir: cand };
        }
    }

    return { pythonPath: isWin ? 'python' : 'python3' };
}

function findServerScript(context: vscode.ExtensionContext): string | undefined {
    const configuredScript = vscode.workspace.getConfiguration('serenitydev').get<string>('serverScript');
    const candidates = [configuredScript, path.join(context.extensionPath, 'serenitydevserver.py')];
    for (const workspaceFolder of vscode.workspace.workspaceFolders ?? []) {
        candidates.push(path.join(workspaceFolder.uri.fsPath, 'serenitydevserver.py'));
    }

    for (const c of candidates) {
        if (c && fs.existsSync(c)) {
            return c;
        }
    }
    return undefined;
}

async function isServerOnline(): Promise<boolean> {
    try {
        const res = await axios.get(`${API_BASE}/status`, { timeout: 1500 });
        return res.status === 200;
    } catch {
        return false;
    }
}

let serverStartPromise: Promise<boolean> | null = null;

async function ensureServerStarted(context: vscode.ExtensionContext, showOutputOnFailure: boolean = false): Promise<boolean> {
    if (serverStartPromise) {
        return serverStartPromise;
    }

    serverStartPromise = (async () => {
        if (await isServerOnline()) {
            serverOutputChannel.appendLine("[SerenityDev] Server is already running on port 8002.");
            await updateStatusBar();
            return true;
        }

        if (serverProcess && !serverProcess.killed && serverProcess.exitCode === null) {
            serverOutputChannel.appendLine("[SerenityDev] Server process is already running/starting. Waiting for health...");
            for (let attempt = 1; attempt <= 15; attempt++) {
                await new Promise((r) => setTimeout(r, 1000));
                if (await isServerOnline()) {
                    await updateStatusBar();
                    return true;
                }
            }
        }

        const { pythonPath, venvDir } = findPythonInterpreter(context);
        const serverScript = findServerScript(context);

        if (!serverScript) {
            vscode.window.showErrorMessage("SerenityDev Error: 'serenitydevserver.py' not found in extension or workspace.");
            return false;
        }

        const isWin = process.platform === 'win32';
        const spawnEnv: NodeJS.ProcessEnv = { ...process.env, PYTHONUNBUFFERED: '1' };
        if (venvDir) {
            spawnEnv.VIRTUAL_ENV = venvDir;
            const binDir = isWin ? path.join(venvDir, 'Scripts') : path.join(venvDir, 'bin');
            spawnEnv.PATH = binDir + (isWin ? ';' : ':') + (spawnEnv.PATH || '');
        }

        const config = vscode.workspace.getConfiguration('serenitydev');
        const customModelsPath = config.get<string>('modelsPath');
        if (customModelsPath) {
            spawnEnv.SERENITY_MODELS_PATH = customModelsPath;
        }
        const workspaceDirs = vscode.workspace.workspaceFolders?.map(f => f.uri.fsPath) ?? [];
        if (workspaceDirs.length > 0) {
            spawnEnv.SERENITY_WORKSPACE_DIR = workspaceDirs.join(path.delimiter);
        }
        const serverCwd = path.dirname(serverScript);
        serverOutputChannel.appendLine(`[SerenityDev] Booting server via ${pythonPath} at ${serverScript}...`);

        try {
            serverProcess = cp.spawn(pythonPath, [serverScript], { cwd: serverCwd, env: spawnEnv, windowsHide: true });

            if (serverProcess.stdout) {
                serverProcess.stdout.on('data', (d: Buffer | string) => {
                    serverOutputChannel.append(d.toString());
                });
            }
            if (serverProcess.stderr) {
                serverProcess.stderr.on('data', (d: Buffer | string) => {
                    serverOutputChannel.append(d.toString());
                });
            }
            serverProcess.on('error', (err: Error) => {
                serverOutputChannel.appendLine(`[SerenityDev Error] Server process error: ${err.message}`);
                if (showOutputOnFailure) {
                    serverOutputChannel.show(true);
                }
            });
            serverProcess.on('close', (code: number | null) => {
                serverOutputChannel.appendLine(`[SerenityDev] Server process terminated with exit code ${code}`);
                serverProcess = undefined;
                updateStatusBar();
            });
        } catch (err: any) {
            serverOutputChannel.appendLine(`[SerenityDev Error] Failed to spawn process: ${err.message}`);
            if (showOutputOnFailure) {
                serverOutputChannel.show();
            }
            return false;
        }

        for (let attempt = 1; attempt <= 20; attempt++) {
            await new Promise((r) => setTimeout(r, 1000));
            if (await isServerOnline()) {
                serverOutputChannel.appendLine("[SerenityDev] Server successfully connected on port 8002.");
                await updateStatusBar();
                return true;
            }
        }

        serverOutputChannel.appendLine("[SerenityDev Warning] Server did not respond within 20 seconds. Check logs.");
        if (showOutputOnFailure) {
            serverOutputChannel.show();
        }
        return false;
    })();

    try {
        return await serverStartPromise;
    } finally {
        serverStartPromise = null;
    }
}

let isPolling = false;
async function updateStatusBar() {
    if (!statusBarItem || isPolling) { return; }
    isPolling = true;
    try {
        const response = await axios.get(`${API_BASE}/status`, { timeout: 2500 });
        const status = response.data?.status;
        const currentModel = response.data?.current_model || 'Supervisor';
        const modelShort = String(currentModel).split('\\').pop()?.split('/').pop()?.replace('.gguf', '') || currentModel;

        if (status === 'online') {
            statusBarItem.text = `$(check) Serenity: ${modelShort}`;
            statusBarItem.tooltip = `SerenityDev Server Online (${currentModel})\nClick for Control Panel`;
            statusBarItem.backgroundColor = undefined;
        } else if (status === 'paused') {
            statusBarItem.text = `$(pause) Serenity: Paused`;
            statusBarItem.tooltip = `SerenityDev Server Paused\nClick to Resume`;
            statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        } else {
            statusBarItem.text = `$(question) Serenity: Unknown`;
            statusBarItem.tooltip = 'SerenityDev Server returned an unknown status\nClick for Control Panel';
            statusBarItem.backgroundColor = undefined;
        }
    } catch {
        statusBarItem.text = `$(error) Serenity: Offline`;
        statusBarItem.tooltip = `SerenityDev Server Offline\nClick to Start Server`;
        statusBarItem.backgroundColor = undefined;
    } finally {
        isPolling = false;
    }
    statusBarItem.show();
}

export function activate(context: vscode.ExtensionContext) {
    serverOutputChannel = vscode.window.createOutputChannel("Serenity Server");
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.text = `$(check) Serenity: Active`;
    statusBarItem.command = 'serenity.showMenu';
    statusBarItem.show();

    context.subscriptions.push(serverOutputChannel, statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand('serenity.keepEdit', async (backupId: string) => {
            try {
                await axios.post(`${API_BASE}/edit/keep`, { backup_id: backupId });
                vscode.window.showInformationMessage(`✅ Edit kept (Backup: ${backupId})`);
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to keep edit: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.revertEdit', async (backupId: string) => {
            try {
                await axios.post(`${API_BASE}/edit/revert`, { backup_id: backupId });
                vscode.window.showWarningMessage(`❌ File edit reverted to original state.`);
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to revert edit: ${err.message}`);
            }
        })
    );

    const chatParticipant = vscode.chat.createChatParticipant('serenitydev.assistant', async (request: vscode.ChatRequest, chatContext: vscode.ChatContext, response: vscode.ChatResponseStream, token: vscode.CancellationToken) => {
        response.progress('Initiating SerenityDev Orchestration Pipeline...');

        if (!(await ensureServerStarted(context, true))) {
            response.markdown(createSafeMarkdown('❌ **SerenityDev server is unavailable.** Check the Serenity Server output for details.'));
            return;
        }

        return new Promise<void>((resolve) => {
            const postData = JSON.stringify({
                prompt: request.prompt,
                session_id: 'native_chat',
                workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
            });

            const req = http.request({
                hostname: SERVER_HOST,
                port: SERVER_PORT,
                path: '/ask_stream',
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(postData)
                }
            }, (res: http.IncomingMessage) => {
                let buffer = '';
                res.on('data', (chunk: any) => {
                    buffer += chunk.toString();
                    let lines = buffer.split('\n');
                    buffer = lines.pop() || '';

                    for (const line of lines) {
                        parseSseLine(line, (data) => {
                            if (data.type === 'progress' && typeof data.text === 'string') {
                                response.progress(data.text);
                            } else if (data.type === 'content' && typeof data.content === 'string') {
                                response.markdown(createSafeMarkdown(data.content));
                            } else if (data.type === 'error' && typeof data.detail === 'string') {
                                response.markdown(createSafeMarkdown(`❌ **Error:** ${data.detail}`));
                            }
                        });
                    }
                });

                res.on('end', () => {
                    if (buffer.trim()) {
                        parseSseLine(buffer, (data) => {
                            if (data.type === 'content' && typeof data.content === 'string') {
                                response.markdown(createSafeMarkdown(data.content));
                            }
                        });
                    }
                    if (res.statusCode && res.statusCode >= 400) {
                        response.markdown(createSafeMarkdown(`❌ **Server returned HTTP ${res.statusCode}.**`));
                    }
                    resolve();
                });
            });

            req.on('error', (err: any) => {
                response.markdown(createSafeMarkdown(`❌ **Server Communication Error:** ${err.message}`));
                resolve();
            });

            token.onCancellationRequested(() => {
                req.destroy();
                resolve();
            });

            req.write(postData);
            req.end();
        });
    });

    chatParticipant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'serenity_icon.png');
    context.subscriptions.push(chatParticipant);

    const lmProvider: vscode.LanguageModelChatProvider = {
        async provideLanguageModelChatInformation(options: any, token: vscode.CancellationToken): Promise<vscode.LanguageModelChatInformation[]> {
            try {
                if (!(await ensureServerStarted(context))) { return []; }
                const response = await axios.get(`${API_BASE}/models`, { timeout: 3000 });
                if (response.data && Array.isArray(response.data.models)) {
                    return response.data.models
                        .filter((m: any) => m && typeof m.id === 'string' && m.id.length > 0)
                        .map((m: any) => ({
                            id: m.id,
                            name: typeof m.name === 'string' && m.name.length > 0 ? m.name : m.id,
                            family: typeof m.family === 'string' && m.family.length > 0 ? m.family : 'serenity-supervisor',
                            version: typeof m.version === 'string' && m.version.length > 0 ? m.version : '1.0.0',
                            maxInputTokens: typeof m.maxInputTokens === 'number' && m.maxInputTokens > 0 ? m.maxInputTokens : 16384,
                            maxOutputTokens: typeof m.maxOutputTokens === 'number' && m.maxOutputTokens > 0 ? m.maxOutputTokens : 16384,
                            capabilities: {
                                toolCalling: m.capabilities?.toolCalling === true,
                                imageInput: m.capabilities?.imageInput === true
                            }
                        })) as vscode.LanguageModelChatInformation[];
                }
            } catch (err) {
                console.error("Failed to fetch models from devserver:", err);
            }
            return [];
        },

        async provideLanguageModelChatResponse(
            model: vscode.LanguageModelChatInformation,
            messages: readonly vscode.LanguageModelChatRequestMessage[],
            options: vscode.ProvideLanguageModelChatResponseOptions,
            progress: vscode.Progress<vscode.LanguageModelResponsePart>,
            token: vscode.CancellationToken
        ): Promise<void> {
            if (!(await ensureServerStarted(context, true))) {
                progress.report(new vscode.LanguageModelTextPart('❌ SerenityDev server is unavailable.'));
                return;
            }

            let prompt = '';
            for (const msg of messages) {
                let textContent = '';
                for (const part of msg.content || []) {
                    if (part instanceof vscode.LanguageModelTextPart) {
                        textContent += part.value;
                    } else if (part && typeof (part as any).value === 'string') {
                        textContent += (part as any).value;
                    } else if (part && typeof (part as any).text === 'string') {
                        textContent += (part as any).text;
                    } else if (typeof part === 'string') {
                        textContent += part;
                    }
                }

                if (msg.role === vscode.LanguageModelChatMessageRole.User) {
                    prompt += `User: ${textContent}\n`;
                } else if (msg.role === vscode.LanguageModelChatMessageRole.Assistant) {
                    prompt += `Assistant: ${textContent}\n`;
                } else {
                    prompt += `System: ${textContent}\n`;
                }
            }

            return new Promise<void>((resolve) => {
                const postData = JSON.stringify({
                    prompt: prompt,
                    model: model.id,
                    session_id: 'native_lm_picker',
                    workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
                });

                const req = http.request({
                    hostname: SERVER_HOST,
                    port: SERVER_PORT,
                    path: '/ask_stream',
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Content-Length': Buffer.byteLength(postData)
                    }
                }, (res: http.IncomingMessage) => {
                    let buffer = '';
                    res.on('data', (chunk: any) => {
                        buffer += chunk.toString();

                        let lines = buffer.split('\n');
                        buffer = lines.pop() || '';

                        for (const line of lines) {
                            parseSseLine(line, (data) => {
                                if (data.type === 'content' && typeof data.content === 'string') {
                                    progress.report(new vscode.LanguageModelTextPart(data.content));
                                } else if (data.type === 'progress' && typeof data.text === 'string') {
                                    progress.report(new vscode.LanguageModelTextPart(`\n_${data.text}_\n\n`));
                                } else if (data.type === 'error' && typeof data.detail === 'string') {
                                    progress.report(new vscode.LanguageModelTextPart(`\n\n❌ **Error:** ${data.detail}\n\n`));
                                }
                            });
                        }
                    });

                    res.on('end', () => {
                        if (buffer.trim()) {
                            parseSseLine(buffer, (data) => {
                                if (data.type === 'content' && typeof data.content === 'string') {
                                    progress.report(new vscode.LanguageModelTextPart(data.content));
                                } else if (data.type === 'progress' && typeof data.text === 'string') {
                                    progress.report(new vscode.LanguageModelTextPart(`\n_${data.text}_\n\n`));
                                }
                            });
                        }
                        if (res.statusCode && res.statusCode >= 400) {
                            progress.report(new vscode.LanguageModelTextPart(`❌ SerenityDev server returned HTTP ${res.statusCode}.`));
                        }
                        resolve();
                    });
                });

                req.on('error', (err: any) => {
                    const msg = err.code === 'ECONNRESET'
                        ? '⚠️ Backend process terminated unexpectedly. Check terminal logs for CUDA/VRAM crash.'
                        : `❌ Server Communication Error: ${err.message}`;
                    progress.report(new vscode.LanguageModelTextPart(`\n\n${msg}\n`));
                    resolve();
                });

                token.onCancellationRequested(() => {
                    req.destroy();
                    resolve();
                });

                req.write(postData);
                req.end();
            });
        },

        async provideTokenCount(
            model: vscode.LanguageModelChatInformation,
            text: string | vscode.LanguageModelChatRequestMessage,
            token: vscode.CancellationToken
        ): Promise<number> {
            const content = typeof text === 'string' ? text : (text.content || []).map(p => {
                if (p instanceof vscode.LanguageModelTextPart) {
                    return p.value;
                }
                return '';
            }).join('');
            return Math.ceil(content.length / 4);
        }
    };

    const inlineCompletionProvider: vscode.InlineCompletionItemProvider = {
        async provideInlineCompletionItems(document, position, _context, token) {
            if (!(await ensureServerStarted(context))) { return []; }
            const text = document.getText();
            const offset = document.offsetAt(position);
            if (token.isCancellationRequested) { return []; }

            const controller = new AbortController();
            const cancellationSubscription = token.onCancellationRequested(() => controller.abort());
            try {
                const response = await axios.post<{ completion?: string }>(
                    `${SERVER_BASE}/fim`,
                    { prefix: text.slice(0, offset), suffix: text.slice(offset) },
                    { timeout: 60000, signal: controller.signal }
                );
                const completion = response.data?.completion;
                if (typeof completion !== 'string' || completion.length === 0) { return []; }
                return [new vscode.InlineCompletionItem(completion, new vscode.Range(position, position))];
            } catch (error) {
                console.error(`SerenityDev inline completion failed: ${getErrorMessage(error)}`);
                return [];
            } finally {
                cancellationSubscription.dispose();
            }
        }
    };

    context.subscriptions.push(
        vscode.lm.registerLanguageModelChatProvider('serenitydev', lmProvider),
        vscode.languages.registerInlineCompletionItemProvider({ pattern: '**' }, inlineCompletionProvider)
    );

    const provider = new SerenityChatProvider(context.extensionUri, context);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(SerenityChatProvider.viewType, provider)
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('serenity.startServer', async () => {
            vscode.window.showInformationMessage('🚀 Starting SerenityDev Server...');
            const online = await ensureServerStarted(context, true);
            if (online) {
                vscode.window.showInformationMessage('🟢 SerenityDev Server is running and healthy.');
            } else {
                vscode.window.showErrorMessage('❌ Failed to start SerenityDev Server. Check output logs.');
            }
            await updateStatusBar();
        }),
        vscode.commands.registerCommand('serenity.stopServer', async () => {
            if (serverProcess && !serverProcess.killed) {
                serverProcess.kill();
                serverProcess = undefined;
            }
            try {
                await axios.post(`${API_BASE}/control/shutdown`, {}, { timeout: 1000 }).catch(() => {});
            } catch { }
            vscode.window.showInformationMessage('🛑 SerenityDev Server stopped.');
            await updateStatusBar();
        }),
        vscode.commands.registerCommand('serenity.toggleServer', async () => {
            if (await isServerOnline()) {
                await vscode.commands.executeCommand('serenity.stopServer');
            } else {
                await vscode.commands.executeCommand('serenity.startServer');
            }
        }),
        vscode.commands.registerCommand('serenity.selectModel', async () => {
            try {
                const modelsRes = await axios.get(`${API_BASE}/models`);
                interface ModelQuickPickItem extends vscode.QuickPickItem {
                    modelId: string;
                }
                const modelsList = modelsRes.data?.models || [];
                const picks: ModelQuickPickItem[] = modelsList.map((m: any) => ({
                    label: m.name || m.id,
                    description: `ID: ${m.id}`,
                    modelId: m.id
                }));

                if (picks.length === 0) {
                    vscode.window.showWarningMessage('No models currently detected. Try adding a custom model folder.');
                    return;
                }

                const chosen = await vscode.window.showQuickPick<ModelQuickPickItem>(picks, {
                    placeHolder: 'Select Active Model for SerenityDev Orchestrator'
                });
                if (!chosen) { return; }

                await axios.post(`${API_BASE}/config`, { current_model: chosen.modelId });
                vscode.window.showInformationMessage(`✅ Active Model switched to: ${chosen.label}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to switch active model: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.scanModels', async () => {
            try {
                await axios.post(`${API_BASE}/register`);
                await new Promise(r => setTimeout(r, 600));
                const modelsRes = await axios.get(`${API_BASE}/models`);
                const modelsList = modelsRes.data?.models || [];
                vscode.window.showInformationMessage(`🔍 Scan Complete: Detected ${modelsList.length} local models.`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Model scan failed: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.explainPlanning', () => {
            const panel = vscode.window.createWebviewPanel(
                'serenityPlanningExplainer',
                'SerenityDev: Planning & Orchestration Architecture',
                vscode.ViewColumn.Beside,
                { enableScripts: true, retainContextWhenHidden: true }
            );
            panel.webview.html = getPlanningExplainerHtml();
        }),
        vscode.commands.registerCommand('serenity.restartServer', async () => {
            try {
                await axios.post(`${API_BASE}/restart`);
                vscode.window.showInformationMessage('🔄 SerenityDev Server restarted. Internal state reset.');
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to restart SerenityDev server: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.unloadModel', async () => {
            try {
                await axios.post(`${API_BASE}/control/unload`);
                vscode.window.showInformationMessage('🧹 SerenityDev: Active models unloaded. VRAM freed.');
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to unload models: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setKVCache', async () => {
            const kChoice = await vscode.window.showQuickPick([
                'f16 (Standard Unquantized)',
                'q8_0 (8-bit Quantized - Medium Saving)',
                'q4_0 (4-bit Quantized - Maximum Saving)',
                'q5_1 (5-bit Quantized - Balanced)'
            ], { placeHolder: 'Select Key Cache Quantization (K)' });
            if (!kChoice) { return; }

            const vChoice = await vscode.window.showQuickPick([
                'f16 (Standard Unquantized)',
                'q8_0 (8-bit Quantized - Medium Saving)',
                'q4_0 (4-bit Quantized - Maximum Saving)',
                'q5_1 (5-bit Quantized - Balanced)'
            ], { placeHolder: 'Select Value Cache Quantization (V)' });
            if (!vChoice) { return; }

            const kVal = kChoice.split(' ')[0];
            const vVal = vChoice.split(' ')[0];
            try {
                await axios.post(`${API_BASE}/config`, { cache_type_k: kVal, cache_type_v: vVal });
                vscode.window.showInformationMessage(`⚙️ KV Cache updated: K=${kVal}, V=${vVal}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to update KV Cache: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setContextSize', async () => {
            const ctxChoice = await vscode.window.showQuickPick([
                '2048 (Low Memory)',
                '4096 (Standard)',
                '8192 (Medium)',
                '16384 (Default 16k)',
                '32768 (Large 32k)',
                '65536 (Huge 64k)',
                '98304 (96k Extended)',
                '128000 (Maximum 128k)',
                '262144 (Ultra 256k)',
                'Custom Value...'
            ], { placeHolder: 'Select Context Window Size (tokens)' });
            if (!ctxChoice) { return; }

            let ctxSize = 16384;
            if (ctxChoice.startsWith('Custom')) {
                const input = await vscode.window.showInputBox({
                    prompt: 'Enter context window size in tokens (e.g. 8192, 32768)',
                    validateInput: (v) => (!isNaN(Number(v)) && Number(v) >= 512 ? null : 'Must be integer >= 512')
                });
                if (!input) { return; }
                ctxSize = parseInt(input, 10);
            } else {
                ctxSize = parseInt(ctxChoice.split(' ')[0], 10);
            }

            try {
                await axios.post(`${API_BASE}/config`, { context_window: ctxSize });
                vscode.window.showInformationMessage(`📐 Context Window set to: ${ctxSize} tokens`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to update context size: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setGpuLayers', async () => {
            const input = await vscode.window.showInputBox({
                prompt: 'Enter GPU Layer Offload Count (-1 for Auto/Dynamic VRAM Guard, 0 for CPU only, or any layer count)',
                placeHolder: '-1 (Auto), 0 (CPU), or any integer (e.g. 33, 60)',
                validateInput: (v) => {
                    const clean = v.trim().toLowerCase();
                    if (clean === 'auto' || clean === '-1') {
                        return null;
                    }
                    if (!isNaN(Number(clean)) && Number(clean) >= 0) {
                        return null;
                    }
                    return 'Must be "Auto", "-1", or an integer >= 0';
                }
            });
            if (input === undefined) { return; }

            const clean = input.trim().toLowerCase();
            const layers = (clean === 'auto' || clean === '-1') ? -1 : parseInt(clean, 10);

            try {
                await axios.post(`${API_BASE}/config`, { gpu_layers: layers });
                const label = layers === -1 ? 'Auto (Dynamic VRAM Guard)' : `${layers} layers`;
                vscode.window.showInformationMessage(`⚡ GPU Layer Offload set to: ${label}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to update GPU layers: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setReasoningStrength', async () => {
            const choice = await vscode.window.showQuickPick([
                { label: 'Low', description: 'Concise thoughts, minimal intermediate token generation', value: 'low' },
                { label: 'Medium', description: 'Balanced step-by-step reasoning and verification', value: 'medium' },
                { label: 'High', description: 'Deep architectural reasoning, edge case analysis', value: 'high' },
                { label: 'XHigh', description: 'Exhaustive multi-perspective breakdown and validation', value: 'xhigh' }
            ], { placeHolder: 'Select Reasoning Strength (Thought Depth)' });
            if (!choice) { return; }

            try {
                await axios.post(`${API_BASE}/config`, { reasoning_strength: choice.value });
                vscode.window.showInformationMessage(`💭 Reasoning Strength set to: ${choice.label}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to update reasoning strength: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setLimitTier', async () => {
            const choice = await vscode.window.showQuickPick([
                { label: 'Default (16 turns)', description: 'Standard balanced execution loop cap', value: 'default' },
                { label: 'Low (8 turns)', description: 'Fast token-efficient single-task turn cap', value: 'low' },
                { label: 'Medium (25 turns)', description: 'Multi-step planning and editing turn cap', value: 'medium' },
                { label: 'High (50 turns)', description: 'Complex deep-dive refactoring turn cap', value: 'high' },
                { label: '⚡ Autonomy (Unlimited)', description: 'Unrestricted 1000+ loops, auto-continue active, max subagents', value: 'autonomy' }
            ], { placeHolder: 'Select Execution Limit Tier' });
            if (!choice) { return; }

            try {
                await axios.post(`${API_BASE}/config`, { limit_tier: choice.value });
                vscode.window.showInformationMessage(`🎯 Limit Tier set to: ${choice.label}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to update limit tier: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.manageMemory', async () => {
            try {
                const memRes = await axios.get(`${API_BASE}/memory`);
                const memories = memRes.data?.memories || [];
                const items: vscode.QuickPickItem[] = [
                    { label: '➕ Store New Memory...', description: 'Add a persistent architectural fact or preference' },
                    { label: '🧹 Purge All Long-Term Memories', description: `Delete all ${memories.length} entries from database` }
                ];
                memories.forEach((m: any) => {
                    items.push({
                        label: `[${(m.category || 'general').toUpperCase()}] ${m.key}`,
                        description: m.content
                    });
                });

                const selection = await vscode.window.showQuickPick(items, {
                    placeHolder: `Long-Term Memory Database (${memories.length} entries)`
                });
                if (!selection) { return; }

                if (selection.label.startsWith('➕')) {
                    const keyInput = await vscode.window.showInputBox({ prompt: 'Memory Key (e.g. preferred_framework, auth_pattern)' });
                    if (!keyInput) { return; }
                    const catInput = await vscode.window.showQuickPick(['architecture', 'decisions', 'code_patterns', 'preferences', 'general'], { placeHolder: 'Select Category' });
                    if (!catInput) { return; }
                    const contentInput = await vscode.window.showInputBox({ prompt: 'Memory Content to persist across agent turns' });
                    if (!contentInput) { return; }
                    await axios.post(`${API_BASE}/memory`, { key: keyInput, category: catInput, content: contentInput });
                    vscode.window.showInformationMessage(`🧠 Saved persistent memory: ${keyInput}`);
                } else if (selection.label.startsWith('🧹')) {
                    const confirm = await vscode.window.showWarningMessage('Are you sure you want to purge all long-term memories?', { modal: true }, 'Purge All');
                    if (confirm === 'Purge All') {
                        await axios.delete(`${API_BASE}/memory`);
                        vscode.window.showInformationMessage('🧠 Purged all long-term memories.');
                    }
                } else {
                    const rawKey = selection.label.split('] ')[1] || selection.label;
                    const action = await vscode.window.showQuickPick(['Delete Memory', 'Keep / Cancel'], { placeHolder: `Action for memory '${rawKey}'` });
                    if (action === 'Delete Memory') {
                        await axios.delete(`${API_BASE}/memory/${encodeURIComponent(rawKey)}`);
                        vscode.window.showInformationMessage(`🗑️ Deleted memory: ${rawKey}`);
                    }
                }
            } catch (err: any) {
                vscode.window.showErrorMessage(`Memory management error: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.purgeSession', async () => {
            try {
                await axios.delete(`${API_BASE}/session/clear`);
                vscode.window.showInformationMessage('🧹 Ephemeral session memories purged.');
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to purge session: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.setRoleModel', async () => {
            const roles = [
                { label: '👑 Supervisor (Low Effort)', key: 'supervisor_low', desc: 'Fast, token-efficient (8 max steps)' },
                { label: '👑 Supervisor (High Effort)', key: 'supervisor_high', desc: 'Deep reasoning (25 max steps)' },
                { label: '⚡ Orchestrator (Turbo Effort)', key: 'orchestrator_turbo', desc: 'Autonomous multi-turn loop (100 max steps)' },
                { label: '🛠️ Worker 1 (Architecture & Reasoning)', key: 'w1_reasoning', desc: 'Complex reasoning & system architecture' },
                { label: '💻 Worker 2 (Heavy Code Synthesis)', key: 'w2_code', desc: 'Precise code writing and file editing' },
                { label: '⚡ Worker 3 (Fast Utilities & Scripts)', key: 'w3_fast', desc: 'Fast file inspection and execution' },
                { label: '🧩 Worker 4 (Specialized Worker)', key: 'w4_specialized', desc: 'Secondary specialized tasks' },
                { label: '✍️ FIM (Inline Autocomplete)', key: 'fim', desc: 'Fast fill-in-the-middle autocomplete' }
            ];

            const rolePick = await vscode.window.showQuickPick(
                roles.map(r => ({ label: r.label, description: r.desc, key: r.key })),
                { placeHolder: 'Select Role / Effort Level to Assign Model' }
            );
            if (!rolePick) { return; }

            try {
                const modelsRes = await axios.get(`${API_BASE}/models`);
                interface ModelQuickPickItem extends vscode.QuickPickItem {
                    modelId: string;
                }
                const modelsList = modelsRes.data?.models || [];
                const modelPicks: ModelQuickPickItem[] = modelsList.map((m: any) => ({
                    label: m.name || m.id,
                    description: `ID: ${m.id}`,
                    modelId: m.id
                }));

                if (modelPicks.length === 0) {
                    vscode.window.showWarningMessage('No local models found to assign.');
                    return;
                }

                const chosenModel = await vscode.window.showQuickPick<ModelQuickPickItem>(modelPicks, {
                    placeHolder: `Select Model to assign to ${rolePick.label}`
                });
                if (!chosenModel) { return; }

                await axios.post(`${API_BASE}/config`, {
                    roles: { [rolePick.key]: chosenModel.modelId }
                });
                vscode.window.showInformationMessage(`🎭 Assigned '${chosenModel.label}' to ${rolePick.label}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to assign model: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.toggleAutoContinue', async () => {
            try {
                const cfgRes = await axios.get(`${API_BASE}/config`);
                const currentAuto = cfgRes.data?.auto_continue || false;
                const newAuto = !currentAuto;
                await axios.post(`${API_BASE}/config`, { auto_continue: newAuto });
                const statusStr = newAuto ? 'ENABLED (Unlimited Iteration up to 1000 loops)' : 'DISABLED (Standard step caps)';
                vscode.window.showInformationMessage(`♾️ Auto-Continue ${statusStr}`);
                await updateStatusBar();
            } catch (err: any) {
                vscode.window.showErrorMessage(`Failed to toggle Auto-Continue: ${err.message}`);
            }
        }),
        vscode.commands.registerCommand('serenity.addModelFolder', async () => {
            const folderUri = await vscode.window.showOpenDialog({
                canSelectFiles: false,
                canSelectFolders: true,
                canSelectMany: false,
                openLabel: 'Select Model Folder'
            });
            if (folderUri && folderUri.length > 0) {
                const folderPath = folderUri[0].fsPath;
                try {
                    await axios.post(`${API_BASE}/config`, { custom_models_dir: folderPath });
                    const config = vscode.workspace.getConfiguration('serenitydev');
                    await config.update('modelsPath', folderPath, vscode.ConfigurationTarget.Global);
                    vscode.window.showInformationMessage(`📁 Added custom model folder: ${folderPath}`);
                    await updateStatusBar();
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to add model folder: ${err.message}`);
                }
            }
        }),
        vscode.commands.registerCommand('serenity.showMenu', async () => {
            const options = [
                '🚀 Start SerenityDev Server',
                '🎯 Select Active Model...',
                '💭 Set Reasoning Strength (Thoughts)...',
                '🎯 Set Limit Tier (Execution Bounds)...',
                '🧠 Manage Long-Term Memory...',
                '🧹 Purge Current Session Memory',
                '🔍 Scan & Detect Local GGUF Models',
                '🎭 Assign Models to Roles / Effort Levels...',
                '♾️ Toggle Auto-Continue (Unlimited Iteration)',
                'ℹ️ Explain SerenityDev Architecture',
                '🔄 Restart Server',
                '🧹 Unload Model (Free VRAM)',
                '⚙️ Set K/V Cache Quantization...',
                '📐 Set Context Window Size...',
                '⚡ Set GPU Layer Offload (Text Box Input)...',
                '⏸️ Pause Server',
                '▶️ Resume Server',
                '📁 Add Custom GGUF Model Folder...'
            ];
            const selection = await vscode.window.showQuickPick(options, {
                placeHolder: 'SerenityDev Server & Orchestration Control Panel'
            });

            if (!selection) { return; }

            if (selection.includes('Start SerenityDev Server')) {
                vscode.commands.executeCommand('serenity.startServer');
            } else if (selection.includes('Select Active Model')) {
                vscode.commands.executeCommand('serenity.selectModel');
            } else if (selection.includes('Set Reasoning Strength')) {
                vscode.commands.executeCommand('serenity.setReasoningStrength');
            } else if (selection.includes('Set Limit Tier')) {
                vscode.commands.executeCommand('serenity.setLimitTier');
            } else if (selection.includes('Manage Long-Term Memory')) {
                vscode.commands.executeCommand('serenity.manageMemory');
            } else if (selection.includes('Purge Current Session')) {
                vscode.commands.executeCommand('serenity.purgeSession');
            } else if (selection.includes('Scan & Detect')) {
                vscode.commands.executeCommand('serenity.scanModels');
            } else if (selection.includes('Explain SerenityDev')) {
                vscode.commands.executeCommand('serenity.explainPlanning');
            } else if (selection.includes('Assign Models to Roles')) {
                vscode.commands.executeCommand('serenity.setRoleModel');
            } else if (selection.includes('Toggle Auto-Continue')) {
                vscode.commands.executeCommand('serenity.toggleAutoContinue');
            } else if (selection.includes('Restart Server')) {
                vscode.commands.executeCommand('serenity.restartServer');
            } else if (selection.includes('Unload Model')) {
                vscode.commands.executeCommand('serenity.unloadModel');
            } else if (selection.includes('Set K/V Cache')) {
                vscode.commands.executeCommand('serenity.setKVCache');
            } else if (selection.includes('Set Context Window')) {
                vscode.commands.executeCommand('serenity.setContextSize');
            } else if (selection.includes('Set GPU Layer')) {
                vscode.commands.executeCommand('serenity.setGpuLayers');
            } else if (selection.includes('Pause Server')) {
                try {
                    await axios.post(`${API_BASE}/control/pause`);
                    vscode.window.showWarningMessage('⏸️ Serenity Server Paused');
                    await updateStatusBar();
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to pause server: ${err.message}`);
                }
            } else if (selection.includes('Resume Server')) {
                try {
                    await axios.post(`${API_BASE}/control/resume`);
                    vscode.window.showInformationMessage('▶️ Serenity Server Resumed');
                    await updateStatusBar();
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to resume server: ${err.message}`);
                }
            } else if (selection.includes('Add Custom GGUF')) {
                vscode.commands.executeCommand('serenity.addModelFolder');
            }
        })
    );

    const interval = setInterval(updateStatusBar, 5000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
    updateStatusBar();
    ensureServerStarted(context);
}

class SerenityChatProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'serenity.chatView';
    private _view?: vscode.WebviewView;
    private _sessionId: string;

    private _generateSessionId(): string {
        return `session_${randomBytes(16).toString('hex')}`;
    }

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _context: vscode.ExtensionContext
    ) {
        this._sessionId = this._generateSessionId();
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        webviewView.webview.onDidReceiveMessage(async (data: any) => {
            if (data.type === 'sendQuery') {
                try {
                    const statusInterval = setInterval(async () => {
                        try {
                            const statusRes = await axios.get(`${API_BASE}/status`, { timeout: 1000 });
                            const statusData = statusRes.data;
                            if (statusData.logs && statusData.logs.length > 0) {
                                this._view?.webview.postMessage({
                                    type: 'updateStatus',
                                    logs: statusData.logs.slice(-5)
                                });
                            }
                        } catch (e) { }
                    }, 800);

                    const postPayload: any = {
                        prompt: data.value,
                        session_id: this._sessionId,
                        workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
                    };
                    if (data.model) {
                        postPayload.model = data.model;
                    }
                    const response = await axios.post(ASK_URL, postPayload, { timeout: 300000 });

                    clearInterval(statusInterval);
                    this._view?.webview.postMessage({ type: 'statusDone' });

                    this._view?.webview.postMessage({
                        type: 'addResponse',
                        value: response.data.answer,
                        routing: response.data.routing
                    });
                } catch (err: any) {
                    this._view?.webview.postMessage({ type: 'statusDone' });
                    this._view?.webview.postMessage({
                        type: 'addError',
                        value: err.message || 'Inference pipeline failure.'
                    });
                }
            } else if (data.type === 'clearSession') {
                try {
                    await axios.delete(`${API_BASE}/session/${this._sessionId}`);
                } catch (e) { }
                this._sessionId = this._generateSessionId();
                vscode.window.showInformationMessage('🧹 Ephemeral session cleared.');
            } else if (data.type === 'revertEdit') {
                vscode.commands.executeCommand('serenity.revertEdit', data.backupId);
            } else if (data.type === 'keepEdit') {
                vscode.commands.executeCommand('serenity.keepEdit', data.backupId);
            } else if (data.type === 'fetchConfig') {
                try {
                    const cfgRes = await axios.get(`${API_BASE}/config`, { timeout: 2000 });
                    this._view?.webview.postMessage({
                        type: 'configLoaded',
                        config: cfgRes.data
                    });
                } catch (e) {
                    this._view?.webview.postMessage({
                        type: 'serverOffline'
                    });
                }
            } else if (data.type === 'selectActiveModel') {
                try {
                    await axios.post(`${API_BASE}/config`, { current_model: data.model });
                    vscode.window.showInformationMessage(`🎯 Active Model switched to: ${data.model}`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to switch active model: ${err.message}`);
                }
            } else if (data.type === 'setReasoningStrength') {
                try {
                    await axios.post(`${API_BASE}/config`, { reasoning_strength: data.value });
                    vscode.window.showInformationMessage(`💭 Reasoning Strength: ${data.value.toUpperCase()}`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to set reasoning strength: ${err.message}`);
                }
            } else if (data.type === 'setLimitTier') {
                try {
                    await axios.post(`${API_BASE}/config`, { limit_tier: data.value });
                    vscode.window.showInformationMessage(`🎯 Limit Tier: ${data.value.toUpperCase()}`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to set limit tier: ${err.message}`);
                }
            } else if (data.type === 'setKVCache') {
                try {
                    await axios.post(`${API_BASE}/config`, { cache_type_k: data.k, cache_type_v: data.v });
                    vscode.window.showInformationMessage(`⚙️ KV Cache: K=${data.k}, V=${data.v}`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to set KV cache: ${err.message}`);
                }
            } else if (data.type === 'setContextSize') {
                try {
                    await axios.post(`${API_BASE}/config`, { context_window: parseInt(data.size, 10) });
                    vscode.window.showInformationMessage(`📐 Context Window: ${data.size} tokens`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to set context size: ${err.message}`);
                }
            } else if (data.type === 'setGpuLayers') {
                try {
                    const layers = data.layers === -1 || data.layers === '-1' || String(data.layers).toLowerCase() === 'auto' ? -1 : parseInt(data.layers, 10);
                    await axios.post(`${API_BASE}/config`, { gpu_layers: layers });
                    const label = layers === -1 ? 'Auto (Dynamic VRAM Guard)' : `${layers} layers`;
                    vscode.window.showInformationMessage(`⚡ GPU Layers: ${label}`);
                    await updateStatusBar();
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: cfgRes.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to set GPU layers: ${err.message}`);
                }
            } else if (data.type === 'toggleAutoContinue') {
                try {
                    const cfgRes = await axios.get(`${API_BASE}/config`);
                    const currentAuto = cfgRes.data?.auto_continue || false;
                    const newAuto = !currentAuto;
                    await axios.post(`${API_BASE}/config`, { auto_continue: newAuto });
                    vscode.window.showInformationMessage(`♾️ Auto-Continue ${newAuto ? 'ENABLED' : 'DISABLED'}`);
                    await updateStatusBar();
                    const updated = await axios.get(`${API_BASE}/config`);
                    this._view?.webview.postMessage({ type: 'configLoaded', config: updated.data });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to toggle auto-continue: ${err.message}`);
                }
            } else if (data.type === 'fetchMemories') {
                try {
                    const memRes = await axios.get(`${API_BASE}/memory`);
                    this._view?.webview.postMessage({
                        type: 'memoriesLoaded',
                        memories: memRes.data?.memories || []
                    });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to load memories: ${err.message}`);
                }
            } else if (data.type === 'storeMemory') {
                try {
                    await axios.post(`${API_BASE}/memory`, {
                        key: data.key,
                        category: data.category || 'general',
                        content: data.content
                    });
                    vscode.window.showInformationMessage(`🧠 Stored memory: ${data.key}`);
                    const memRes = await axios.get(`${API_BASE}/memory`);
                    this._view?.webview.postMessage({ type: 'memoriesLoaded', memories: memRes.data?.memories || [] });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to store memory: ${err.message}`);
                }
            } else if (data.type === 'deleteMemory') {
                try {
                    await axios.delete(`${API_BASE}/memory/${encodeURIComponent(data.key)}`);
                    vscode.window.showInformationMessage(`🗑️ Deleted memory: ${data.key}`);
                    const memRes = await axios.get(`${API_BASE}/memory`);
                    this._view?.webview.postMessage({ type: 'memoriesLoaded', memories: memRes.data?.memories || [] });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to delete memory: ${err.message}`);
                }
            } else if (data.type === 'purgeMemories') {
                try {
                    await axios.delete(`${API_BASE}/memory`);
                    vscode.window.showInformationMessage('🧠 Purged all long-term memories.');
                    this._view?.webview.postMessage({ type: 'memoriesLoaded', memories: [] });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to purge memories: ${err.message}`);
                }
            } else if (data.type === 'startServer') {
                vscode.commands.executeCommand('serenity.startServer');
            } else if (data.type === 'stopServer') {
                vscode.commands.executeCommand('serenity.stopServer');
            } else if (data.type === 'toggleServer') {
                vscode.commands.executeCommand('serenity.toggleServer');
            } else if (data.type === 'scanModels') {
                vscode.commands.executeCommand('serenity.scanModels');
            } else if (data.type === 'runCommand') {
                if (data.command) {
                    vscode.commands.executeCommand(`serenity.${data.command}`);
                }
            }
        });
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        return `<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <style>
                :root {
                    --card-bg: var(--vscode-editor-inactiveSelectionBackground, rgba(255,255,255,0.06));
                    --border-color: var(--vscode-widget-border, rgba(255,255,255,0.12));
                    --accent-color: #3b82f6;
                    --autonomy-color: #eab308;
                }
                * { box-sizing: border-box; }
                body {
                    font-family: var(--vscode-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);
                    color: var(--vscode-editor-foreground);
                    padding: 8px;
                    font-size: 11px;
                    display: flex;
                    flex-direction: column;
                    height: 100vh;
                    margin: 0;
                    background: transparent;
                }
                #chatContainer { display: flex; flex-direction: column; height: 100%; width: 100%; overflow: hidden; }
                
                /* Header */
                .header-bar { display: flex; justify-content: space-between; align-items: center; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); margin-bottom: 6px; }
                .title-group { display: flex; align-items: center; gap: 6px; font-weight: 700; font-size: 12px; }
                .status-badge { font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 12px; display: inline-flex; align-items: center; gap: 4px; transition: all 0.2s ease; user-select: none; cursor: pointer; }
                .status-online { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }
                .status-online:hover { background: rgba(34, 197, 94, 0.3); }
                .status-offline { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
                .status-offline:hover { background: rgba(239, 68, 68, 0.3); }

                /* Controls Grid */
                .controls-container { display: flex; flex-direction: column; gap: 4px; margin-bottom: 6px; background: var(--card-bg); padding: 6px; border-radius: 6px; border: 1px solid var(--border-color); }
                .controls-row { display: flex; align-items: center; gap: 6px; }
                .control-group { display: flex; align-items: center; gap: 4px; flex: 1; min-width: 0; }
                .control-label { font-size: 10px; opacity: 0.8; white-space: nowrap; font-weight: 500; }
                .control-select, .control-input {
                    flex: 1;
                    min-width: 0;
                    background: var(--vscode-input-background);
                    color: var(--vscode-input-foreground);
                    border: 1px solid var(--vscode-input-border, var(--border-color));
                    border-radius: 4px;
                    padding: 3px 5px;
                    font-size: 10.5px;
                    font-family: inherit;
                }
                .control-select:focus, .control-input:focus { outline: 1px solid var(--vscode-focusBorder); border-color: var(--vscode-focusBorder); }
                
                /* Toolbar */
                .toolbar { display: flex; flex-wrap: wrap; gap: 4px; padding-bottom: 6px; border-bottom: 1px solid var(--border-color); margin-bottom: 6px; }
                .tool-btn {
                    background: var(--vscode-button-secondaryBackground, rgba(255,255,255,0.08));
                    color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
                    border: 1px solid var(--border-color);
                    font-size: 10px;
                    padding: 3px 8px;
                    border-radius: 4px;
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                    gap: 3px;
                    font-weight: 500;
                    transition: all 0.15s ease;
                }
                .tool-btn:hover { background: var(--vscode-button-hoverBackground); color: var(--vscode-button-foreground); }
                .tool-btn.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); font-weight: 600; }
                .tool-btn.accent { background: #8b5cf6; color: #ffffff; }
                .tool-btn.autonomy { background: rgba(234, 179, 8, 0.2); color: #facc15; border-color: rgba(234, 179, 8, 0.4); font-weight: 600; }
                .tool-btn.active { background: #10b981; color: white; }

                /* Chat Output */
                #output { flex: 1; overflow-y: auto; padding: 4px; margin-bottom: 6px; display: flex; flex-direction: column; gap: 8px; }
                .msg { padding: 8px 10px; border-radius: 6px; line-height: 1.45; word-break: break-word; }
                .msg.user { background: var(--vscode-button-background); color: var(--vscode-button-foreground); align-self: flex-end; max-width: 85%; }
                .msg.agent { background: var(--card-bg); border: 1px solid var(--border-color); align-self: flex-start; width: 100%; }
                
                /* Thought Accordion & Subagent Cards */
                details.thought-box {
                    background: rgba(0, 0, 0, 0.25);
                    border: 1px solid var(--border-color);
                    border-radius: 4px;
                    padding: 4px 8px;
                    margin: 6px 0;
                    font-family: var(--vscode-editor-font-family, monospace);
                    font-size: 10px;
                }
                details.thought-box summary { cursor: pointer; color: #a78bfa; font-weight: 600; user-select: none; }
                details.thought-box pre { margin: 4px 0 0 0; white-space: pre-wrap; color: #94a3b8; }
                
                .subagent-card {
                    background: rgba(59, 130, 246, 0.1);
                    border-left: 3px solid #3b82f6;
                    padding: 6px 8px;
                    margin: 4px 0;
                    border-radius: 0 4px 4px 0;
                    font-size: 10px;
                }
                .routing-box {
                    font-family: var(--vscode-editor-font-family, monospace);
                    font-size: 10px;
                    background: rgba(0,0,0,0.25);
                    border-left: 3px solid var(--vscode-textLink-foreground, #38bdf8);
                    padding: 6px 8px;
                    margin-top: 6px;
                    border-radius: 0 4px 4px 0;
                }

                /* Input Area */
                #inputContainer { display: flex; gap: 4px; align-items: flex-end; }
                textarea#promptInput {
                    flex: 1;
                    background: var(--vscode-input-background);
                    color: var(--vscode-input-foreground);
                    border: 1px solid var(--vscode-input-border, var(--border-color));
                    resize: none;
                    border-radius: 6px;
                    font-family: inherit;
                    font-size: 11.5px;
                    padding: 6px 8px;
                    min-height: 34px;
                    max-height: 120px;
                }
                textarea#promptInput:focus { outline: 1px solid var(--vscode-focusBorder); }
                .action-btn {
                    padding: 6px 12px;
                    border-radius: 6px;
                    font-weight: 600;
                    font-size: 11px;
                    border: none;
                    cursor: pointer;
                    height: 34px;
                }
                .action-btn.ask { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
                .action-btn.ask:hover { background: var(--vscode-button-hoverBackground); }
                .action-btn.clear { background: var(--vscode-button-secondaryBackground, #475569); color: var(--vscode-button-secondaryForeground, white); }

                /* Memory Modal */
                .modal-overlay {
                    display: none;
                    position: fixed;
                    top: 0; left: 0; right: 0; bottom: 0;
                    background: rgba(0,0,0,0.65);
                    z-index: 100;
                    padding: 12px;
                    backdrop-filter: blur(2px);
                }
                .modal-content {
                    background: var(--vscode-editor-background, #1e1e1e);
                    border: 1px solid var(--border-color);
                    border-radius: 8px;
                    height: 90vh;
                    display: flex;
                    flex-direction: column;
                    padding: 12px;
                    gap: 8px;
                }
                .memory-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
                .memory-item { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column; gap: 2px; }
                .memory-item-header { display: flex; justify-content: space-between; align-items: center; }
                .memory-cat-tag { font-size: 9px; text-transform: uppercase; background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 1px 5px; border-radius: 3px; font-weight: 600; }
                .memory-del-btn { background: transparent; border: none; color: #f87171; cursor: pointer; font-size: 11px; padding: 0 4px; }
                .memory-del-btn:hover { color: #ef4444; }
            </style>
        </head>
        <body>
            <div id="chatContainer">
                <div class="header-bar">
                    <div class="title-group">
                        <span>🧠 SerenityDev Core</span>
                    </div>
                    <span id="serverStatusBadge" class="status-badge status-offline" onclick="toggleServerState()" title="Click to toggle SerenityDev server">🔴 Offline (Click to Start)</span>
                </div>

                <!-- Controls Grid Row 1: Model, Reasoning, Limits -->
                <div class="controls-container">
                    <div class="controls-row">
                        <div class="control-group" style="flex: 2;">
                            <span class="control-label">Model:</span>
                            <select id="activeModelSelect" class="control-select" onchange="onModelChange(this)">
                                <option value="">Detecting models...</option>
                            </select>
                        </div>
                        <div class="control-group" style="flex: 1.2;">
                            <span class="control-label">Reasoning:</span>
                            <select id="reasoningSelect" class="control-select" onchange="onReasoningChange(this)">
                                <option value="low">Low (Concise)</option>
                                <option value="medium" selected>Medium (Standard)</option>
                                <option value="high">High (Deep)</option>
                                <option value="xhigh">XHigh (Max)</option>
                            </select>
                        </div>
                        <div class="control-group" style="flex: 1.2;">
                            <span class="control-label">Limit:</span>
                            <select id="limitTierSelect" class="control-select" onchange="onLimitTierChange(this)">
                                <option value="default" selected>Default (16)</option>
                                <option value="low">Low (8)</option>
                                <option value="medium">Medium (25)</option>
                                <option value="high">High (50)</option>
                                <option value="autonomy">⚡ Autonomy (∞)</option>
                            </select>
                        </div>
                    </div>

                    <!-- Controls Grid Row 2: KV Quant, Context, GPU Layers, Auto-Cont -->
                    <div class="controls-row">
                        <div class="control-group">
                            <span class="control-label">KV Quant:</span>
                            <select id="kvSelect" class="control-select" onchange="onKVChange(this)">
                                <option value="f16" selected>f16</option>
                                <option value="q8_0">q8_0</option>
                                <option value="q5_1">q5_1</option>
                                <option value="q5_0">q5_0</option>
                                <option value="q4_0">q4_0</option>
                                <option value="turbo4_tcq">turbo4_tcq</option>
                            </select>
                        </div>
                        <div class="control-group">
                            <span class="control-label">Ctx:</span>
                            <select id="ctxSelect" class="control-select" onchange="onCtxChange(this)">
                                <option value="2048">2k</option>
                                <option value="4096">4k</option>
                                <option value="8192">8k</option>
                                <option value="16384" selected>16k</option>
                                <option value="32768">32k</option>
                                <option value="65536">64k</option>
                                <option value="131072">128k</option>
                                <option value="262144">256k</option>
                            </select>
                        </div>
                        <div class="control-group">
                            <span class="control-label">GPU:</span>
                            <input type="text" id="gpuLayersInput" class="control-input" placeholder="Auto (-1) or 0-99" title="Type -1 for Auto, 0 for CPU, or any layer count" onchange="onGpuLayersChange(this)" />
                        </div>
                        <button id="autoContBtn" class="tool-btn" onclick="toggleAutoCont()" title="Toggle Auto-Continue unlimited loops">♾️ Auto-Cont</button>
                    </div>
                </div>

                <!-- Toolbar -->
                <div class="toolbar">
                    <button class="tool-btn accent" onclick="openMemoryModal()">🧠 Memory</button>
                    <button class="tool-btn" onclick="sendCmd('setRoleModel')">🎭 Roles</button>
                    <button class="tool-btn" onclick="sendCmd('unloadModel')">🧹 Unload</button>
                    <button class="tool-btn" onclick="sendCmd('restartServer')">🔄 Restart</button>
                    <button class="tool-btn" onclick="sendCmd('scanModels')">🔍 Scan</button>
                    <button class="tool-btn primary" onclick="sendCmd('explainPlanning')">ℹ️ Explain</button>
                </div>
                
                <!-- Chat Output -->
                <div id="output">
                    <div class="msg agent">
                        <strong>SerenityDev:</strong> Core Initialized.<br/>
                        <span style="opacity: 0.8; font-size: 10px;">Select a model or type queries below to execute multi-agent plans.</span>
                    </div>
                </div>
                
                <!-- Input Area -->
                <div id="inputContainer">
                    <textarea id="promptInput" rows="2" placeholder="Ask Serenity to plan, refactor, or edit code... (Enter to submit, Shift+Enter for newline)" onkeydown="handleKeyDown(event)"></textarea>
                    <button class="action-btn ask" onclick="submitQuery()">Ask</button>
                    <button class="action-btn clear" onclick="clearSession()">Clear</button>
                </div>
            </div>

            <!-- Long-Term Memory Modal -->
            <div id="memoryModal" class="modal-overlay">
                <div class="modal-content">
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 6px;">
                        <span style="font-weight: 700; font-size: 12px;">🧠 Long-Term Memory Manager</span>
                        <button class="tool-btn" onclick="closeMemoryModal()">✕ Close</button>
                    </div>
                    <div style="display: flex; gap: 4px;">
                        <input type="text" id="memorySearchInput" class="control-input" placeholder="Search persistent memories..." oninput="filterMemories(this.value)" />
                        <button class="tool-btn" style="color: #f87171;" onclick="purgeAllMemories()">🧹 Purge All</button>
                    </div>
                    <div id="memoryListContainer" class="memory-list">
                        <div style="opacity: 0.7; text-align: center; margin-top: 20px;">Loading persistent memories...</div>
                    </div>
                    <!-- Quick Add Memory -->
                    <div style="border-top: 1px solid var(--border-color); padding-top: 6px; display: flex; flex-direction: column; gap: 4px;">
                        <div style="font-weight: 600; font-size: 10px;">➕ Store Knowledge Fact:</div>
                        <div style="display: flex; gap: 4px;">
                            <input type="text" id="newMemKey" class="control-input" placeholder="Key (e.g. auth_pattern)" style="flex: 1;" />
                            <select id="newMemCat" class="control-select" style="flex: 1;">
                                <option value="architecture">Architecture</option>
                                <option value="decisions">Decisions</option>
                                <option value="code_patterns">Code Patterns</option>
                                <option value="preferences">Preferences</option>
                                <option value="general" selected>General</option>
                            </select>
                        </div>
                        <div style="display: flex; gap: 4px;">
                            <input type="text" id="newMemContent" class="control-input" placeholder="Content to persist..." style="flex: 3;" />
                            <button class="tool-btn primary" onclick="saveNewMemory()">Save</button>
                        </div>
                    </div>
                </div>
            </div>

            <script>
                const vscode = acquireVsCodeApi();
                let globalConfig = null;
                let cachedMemories = [];

                function escapeHtml(str) {
                    if (!str) return '';
                    return String(str)
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#039;');
                }

                function sendCmd(commandName) {
                    vscode.postMessage({ type: 'runCommand', command: commandName });
                }

                function onModelChange(selectEl) {
                    if (selectEl.value) {
                        vscode.postMessage({ type: 'selectActiveModel', model: selectEl.value });
                    }
                }

                function onReasoningChange(selectEl) {
                    vscode.postMessage({ type: 'setReasoningStrength', value: selectEl.value });
                }

                function onLimitTierChange(selectEl) {
                    vscode.postMessage({ type: 'setLimitTier', value: selectEl.value });
                }

                function onKVChange(selectEl) {
                    const val = selectEl.value;
                    vscode.postMessage({ type: 'setKVCache', k: val, v: val });
                }

                function onCtxChange(selectEl) {
                    vscode.postMessage({ type: 'setContextSize', size: selectEl.value });
                }

                function onGpuLayersChange(inputEl) {
                    vscode.postMessage({ type: 'setGpuLayers', layers: inputEl.value.trim() });
                }

                function toggleAutoCont() {
                    vscode.postMessage({ type: 'toggleAutoContinue' });
                }

                let lastPromptText = '';

                function toggleServerState() {
                    const badge = document.getElementById('serverStatusBadge');
                    const isOnline = badge && badge.classList.contains('status-online');
                    if (isOnline) {
                        sendCmd('stopServer');
                    } else {
                        sendCmd('startServer');
                    }
                }

                function handleKeyDown(event) {
                    if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        submitQuery();
                    }
                }

                function submitQuery(retryText) {
                    const input = document.getElementById('promptInput');
                    const text = (typeof retryText === 'string') ? retryText : (input ? input.value.trim() : '');
                    if (!text) return;
                    lastPromptText = text;
                    if (typeof retryText !== 'string' && input) {
                        input.value = '';
                    }

                    const modelSelect = document.getElementById('activeModelSelect');
                    const selectedModel = modelSelect ? modelSelect.value : undefined;

                    const out = document.getElementById('output');
                    out.innerHTML += '<div class="msg user"><strong>You:</strong> ' + escapeHtml(text) + '</div>';
                    out.innerHTML += '<div id="statusBox" class="routing-box">⚙️ Initializing Autonomous Routing Pipeline...</div>';
                    
                    vscode.postMessage({ type: 'sendQuery', value: text, model: selectedModel });
                    out.scrollTop = out.scrollHeight;
                }

                function retryLastQuery() {
                    if (lastPromptText) {
                        submitQuery(lastPromptText);
                    }
                }

                function clearSession() {
                    const out = document.getElementById('output');
                    out.innerHTML = '<div class="msg agent"><strong>SerenityDev:</strong> Session cleared. Fresh context initialized.</div>';
                    vscode.postMessage({ type: 'clearSession' });
                }

                function keepEdit(backupId, btnEl) {
                    vscode.postMessage({ type: 'keepEdit', backupId: backupId });
                    if (btnEl && btnEl.parentElement) {
                        btnEl.parentElement.innerHTML = '<span style="color: #4ade80; font-weight: 600;">✓ Kept</span>';
                    }
                }

                function rejectEdit(backupId, btnEl) {
                    vscode.postMessage({ type: 'revertEdit', backupId: backupId });
                    if (btnEl && btnEl.parentElement) {
                        btnEl.parentElement.innerHTML = '<span style="color: #f87171; font-weight: 600;">❌ Reverted</span>';
                    }
                }

                function parseProofBadges(rawText) {
                    if (!rawText) return '';
                    var badgeRegex = new RegExp('(?:PROOF:\\\\s*)?(edited:[^\\\\s\\\\n\\\\(\\\\)]+)-(\\\\d+)\\\\+(\\\\d+)(?:\\\\s*\\\\(backup:([^\\\\)]+)\\\\))?', 'g');
                    return rawText.replace(badgeRegex, function(match, proof, dels, adds, bakId) {
                        const bId = bakId ? bakId.trim() : '';
                        const safeBId = escapeHtml(bId);
                        const safeProof = escapeHtml(proof);
                        const safeDels = escapeHtml(dels);
                        const safeAdds = escapeHtml(adds);
                        const btnHtml = safeBId ? '<span style="margin-left: 8px;">' +
                            '<button onclick="keepEdit(&quot;' + safeBId + '&quot;, this)" style="background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; margin-right: 4px; font-weight: bold; font-size: 10px;">Keep</button>' +
                            '<button onclick="rejectEdit(&quot;' + safeBId + '&quot;, this)" style="background: #dc2626; color: white; border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-weight: bold; font-size: 10px;">Reject</button>' +
                            '</span>' : '';
                        return '<div style="background: rgba(0,0,0,0.3); border: 1px solid var(--border-color); padding: 5px 8px; border-radius: 4px; margin: 4px 0; font-family: var(--vscode-editor-font-family, monospace); font-size: 10px; display: flex; justify-content: space-between; align-items: center;">' +
                                '<span>📝 <code>' + safeProof + '-' + safeDels + '+' + safeAdds + '</code></span>' + btnHtml + '</div>';
                    });
                }

                /* Memory Modal Helpers */
                function openMemoryModal() {
                    document.getElementById('memoryModal').style.display = 'block';
                    vscode.postMessage({ type: 'fetchMemories' });
                }
                function closeMemoryModal() {
                    document.getElementById('memoryModal').style.display = 'none';
                }
                function renderMemories(memories) {
                    cachedMemories = memories;
                    const container = document.getElementById('memoryListContainer');
                    if (!memories || memories.length === 0) {
                        container.innerHTML = '<div style="opacity: 0.7; text-align: center; margin-top: 20px;">No persistent memories stored.</div>';
                        return;
                    }
                    let html = '';
                    memories.forEach(m => {
                        html += '<div class="memory-item">' +
                            '<div class="memory-item-header">' +
                                '<span style="font-weight: 600;">' + escapeHtml(m.key) + '</span>' +
                                '<div style="display: flex; align-items: center; gap: 4px;">' +
                                    '<span class="memory-cat-tag">' + escapeHtml(m.category || 'general') + '</span>' +
                                    '<button class="memory-del-btn" onclick="deleteMemory(&quot;' + escapeHtml(m.key) + '&quot;)">🗑️</button>' +
                                '</div>' +
                            '</div>' +
                            '<div style="font-size: 10.5px; opacity: 0.9;">' + escapeHtml(m.content) + '</div>' +
                            '</div>';
                    });
                    container.innerHTML = html;
                }
                function filterMemories(query) {
                    if (!query) {
                        renderMemories(cachedMemories);
                        return;
                    }
                    const q = query.toLowerCase();
                    const filtered = cachedMemories.filter(m => 
                        (m.key && m.key.toLowerCase().includes(q)) || 
                        (m.content && m.content.toLowerCase().includes(q)) ||
                        (m.category && m.category.toLowerCase().includes(q))
                    );
                    renderMemories(filtered);
                }
                function saveNewMemory() {
                    const k = document.getElementById('newMemKey').value.trim();
                    const c = document.getElementById('newMemCat').value;
                    const val = document.getElementById('newMemContent').value.trim();
                    if (!k || !val) return;
                    vscode.postMessage({ type: 'storeMemory', key: k, category: c, content: val });
                    document.getElementById('newMemKey').value = '';
                    document.getElementById('newMemContent').value = '';
                }
                function deleteMemory(key) {
                    vscode.postMessage({ type: 'deleteMemory', key: key });
                }
                function purgeAllMemories() {
                    if (confirm('Purge all long-term memories from the database?')) {
                        vscode.postMessage({ type: 'purgeMemories' });
                    }
                }

                vscode.postMessage({ type: 'fetchConfig' });
                setInterval(() => { vscode.postMessage({ type: 'fetchConfig' }); }, 8000);

                window.addEventListener('message', event => {
                    const message = event.data;
                    const out = document.getElementById('output');
                    
                    if (message.type === 'serverOffline') {
                        const badge = document.getElementById('serverStatusBadge');
                        if (badge) {
                            badge.className = 'status-badge status-offline';
                            badge.textContent = '🔴 Offline (Click to Start)';
                        }
                        return;
                    } else if (message.type === 'configLoaded') {
                        globalConfig = message.config;
                        const badge = document.getElementById('serverStatusBadge');
                        const modelSelect = document.getElementById('activeModelSelect');
                        const reasoningSelect = document.getElementById('reasoningSelect');
                        const limitTierSelect = document.getElementById('limitTierSelect');
                        const kvSelect = document.getElementById('kvSelect');
                        const ctxSelect = document.getElementById('ctxSelect');
                        const gpuInput = document.getElementById('gpuLayersInput');
                        const autoContBtn = document.getElementById('autoContBtn');

                        if (badge) {
                            badge.className = 'status-badge status-online';
                            badge.textContent = '🟢 Online (Click to Stop)';
                        }

                        if (globalConfig) {
                            if (modelSelect) {
                                const available = Array.isArray(globalConfig.available_models) ? globalConfig.available_models : [];
                                const current = globalConfig.current_model || '';
                                let html = '';
                                available.forEach(m => {
                                    const sel = current && (m === current || current.startsWith(m) || m.startsWith(current)) ? 'selected' : '';
                                    html += '<option value="' + escapeHtml(m) + '" ' + sel + '>' + escapeHtml(m) + '</option>';
                                });
                                if (available.length === 0 && current) {
                                    html = '<option value="' + escapeHtml(current) + '" selected>' + escapeHtml(current) + '</option>';
                                }
                                modelSelect.innerHTML = html;
                            }
                            if (reasoningSelect && globalConfig.reasoning_strength) {
                                reasoningSelect.value = globalConfig.reasoning_strength;
                            }
                            if (limitTierSelect && globalConfig.limit_tier) {
                                limitTierSelect.value = globalConfig.limit_tier;
                            }
                            if (kvSelect && globalConfig.cache_type_k) {
                                kvSelect.value = globalConfig.cache_type_k;
                            }
                            if (ctxSelect && globalConfig.context_window) {
                                ctxSelect.value = String(globalConfig.context_window);
                            }
                            if (gpuInput) {
                                gpuInput.value = (globalConfig.gpu_layers === null || globalConfig.gpu_layers === -1 || globalConfig.gpu_layers === undefined) ? 'Auto' : String(globalConfig.gpu_layers);
                            }
                            if (autoContBtn) {
                                if (globalConfig.auto_continue) {
                                    autoContBtn.className = 'tool-btn autonomy active';
                                } else {
                                    autoContBtn.className = 'tool-btn';
                                }
                            }
                        }
                        return;
                    } else if (message.type === 'memoriesLoaded') {
                        renderMemories(message.memories || []);
                        return;
                    } else if (message.type === 'updateStatus') {
                        const statusBox = document.getElementById('statusBox');
                        if (statusBox) {
                            let logHtml = '';
                            const logs = Array.isArray(message.logs) ? message.logs : [];
                            logs.forEach((l) => { logHtml += '<div>⚙️ ' + escapeHtml(l) + '</div>'; });
                            statusBox.innerHTML = logHtml;
                        }
                        out.scrollTop = out.scrollHeight;
                        return;
                    } else if (message.type === 'statusDone') {
                        const statusBox = document.getElementById('statusBox');
                        if (statusBox) {
                            statusBox.id = '';
                        }
                        return;
                    } else if (message.type === 'addResponse') {
                        let routingHtml = '';
                        if (message.routing) {
                            let stepList = '';
                            const steps = Array.isArray(message.routing.steps) ? message.routing.steps : [];
                            const stepCount = typeof message.routing.step_count === 'number'
                                ? message.routing.step_count
                                : (Array.isArray(message.routing.steps) ? message.routing.steps.length : 0);
                            if (steps.length > 0) {
                                steps.forEach((s) => {
                                    stepList += '<div style="margin-top: 2px;">🛠️ Step ' + escapeHtml(String(s.step)) + ': <code>' + escapeHtml(String(s.tool)) + '</code> ➡️ ' + parseProofBadges(s.details) + '</div>';
                                });
                            }
                            routingHtml = '<div class="routing-box">🗺️ Execution Plan Turns: ' + stepCount + '<br/>Worker: ' + escapeHtml(String(message.routing.worker || 'Supervisor')) + stepList + '</div>';
                        }
                        const formattedVal = parseProofBadges(message.value);
                        const isErrorMsg = String(message.value).includes('Model Error') || String(message.value).includes('❌');
                        const retryBtn = isErrorMsg ? '<div style="margin-top: 6px;"><button class="tool-btn" style="font-size: 10px; padding: 2px 8px;" onclick="retryLastQuery()">🔄 Retry</button></div>' : '';
                        out.innerHTML += '<div class="msg agent"><strong>SerenityDev:</strong> ' + formattedVal + retryBtn + routingHtml + '</div>';
                    } else if (message.type === 'addError') {
                        out.innerHTML += '<div class="msg" style="color: #f87171">❌ Error: ' + escapeHtml(String(message.value)) + 
                            ' <button class="tool-btn" style="margin-left: 6px; font-size: 10px; padding: 2px 6px;" onclick="retryLastQuery()">🔄 Retry</button></div>';
                    }
                    out.scrollTop = out.scrollHeight;
                });
            </script>
        </body>
        </html>`;
    }
}

function getPlanningExplainerHtml(): string {
    return `<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0d1117; color: #c9d1d9; padding: 24px; line-height: 1.6; max-width: 900px; margin: 0 auto; }
            h1, h2, h3 { color: #58a6ff; font-weight: 600; }
            h1 { border-bottom: 1px solid #30363d; padding-bottom: 10px; font-size: 24px; }
            h2 { font-size: 18px; margin-top: 24px; color: #79c0ff; }
            .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin: 12px 0; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; margin: 12px 0; }
            .code-box { background: #090d13; border: 1px solid #30363d; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 12px; color: #e6edf3; overflow-x: auto; }
            ul { padding-left: 20px; }
            li { margin-bottom: 6px; }
        </style>
    </head>
    <body>
        <h1>🧠 SerenityDev: Architecture & Orchestration</h1>
        <p><strong>SerenityDev</strong> is a high-performance hierarchical multi-agent coordination core.</p>
        <h2>🗺️ 1. Hierarchical Supervisor-Worker Effort Tiers</h2>
        <div class="grid">
            <div class="card">
                <h3>👑 Supervisor (Low Effort)</h3>
                <p>Fast, token-efficient routing and planning capped at 8 max steps.</p>
            </div>
            <div class="card">
                <h3>👑 Supervisor (High Effort)</h3>
                <p>Deep reasoning and comprehensive plan formulation capped at 25 max steps.</p>
            </div>
            <div class="card">
                <h3>⚡ Orchestrator (Turbo Effort)</h3>
                <p>Autonomous multi-agent execution loop with 100 max steps.</p>
            </div>
        </div>
        <h2>⌨️ Quick Commands in VS Code</h2>
        <div class="code-box">
Ctrl+Shift+P -> "Serenity: Start Server"<br/>
Ctrl+Shift+P -> "Serenity: Select Active Model"<br/>
Ctrl+Shift+P -> "Serenity: Scan & Detect Models"<br/>
Ctrl+Shift+P -> "Serenity: Show Control Menu"
        </div>
    </body>
    </html>`;
}

export function deactivate() {
    if (serverProcess) {
        serverProcess.kill();
        serverProcess = undefined;
    }
}