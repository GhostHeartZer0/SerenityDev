import * as vscode from 'vscode';
import axios from 'axios';
import * as http from 'http';
import * as cp from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { randomBytes } from 'crypto';

let serverProcess: cp.ChildProcess | undefined;
let serverOutputChannel: vscode.OutputChannel;
let statusBarItem: vscode.StatusBarItem;

const API_BASE = 'http://localhost:8002/api';
const ASK_URL = 'http://localhost:8002/ask';

function createSafeMarkdown(content: string): vscode.MarkdownString {
    const md = new vscode.MarkdownString(content);
    md.isTrusted = true;
    md.supportHtml = true;
    return md;
}

function findPythonInterpreter(context: vscode.ExtensionContext): { pythonPath: string; venvDir?: string } {
    const config = vscode.workspace.getConfiguration('serenitydev');
    let customPy = config.get<string>('pythonPath');
    if (customPy && customPy !== 'python' && fs.existsSync(customPy)) {
        return { pythonPath: customPy };
    }

    const isWin = process.platform === 'win32';
    const candidateDirs: string[] = [];

    if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
        for (const wf of vscode.workspace.workspaceFolders) {
            candidateDirs.push(path.join(wf.uri.fsPath, '.venv'));
            candidateDirs.push(path.join(wf.uri.fsPath, 'venv'));
        }
    }

    candidateDirs.push(
        path.join(context.extensionPath, '.venv'),
        path.join(context.extensionPath, 'venv'),
        path.join(os.homedir(), 'SerenityDev', '.venv'),
        'C:\\Users\\ccrg6\\SerenityDev\\.venv'
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
    const candidates = [
        path.join(context.extensionPath, 'serenitydevserver.py'),
        path.join(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '', 'serenitydevserver.py'),
        'C:\\Users\\ccrg6\\SerenityDev\\serenitydevserver.py'
    ];

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
        const spawnEnv = { ...process.env };
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

        const serverCwd = path.dirname(serverScript);
        serverOutputChannel.appendLine(`[SerenityDev] Booting server via ${pythonPath} at ${serverScript}...`);

        try {
            serverProcess = cp.spawn(pythonPath, [serverScript], { cwd: serverCwd, env: spawnEnv, windowsHide: true });

            if (serverProcess.stdout) {
                serverProcess.stdout.on('data', (d) => {
                    serverOutputChannel.append(d.toString());
                });
            }
            if (serverProcess.stderr) {
                serverProcess.stderr.on('data', (d) => {
                    serverOutputChannel.append(d.toString());
                });
            }
            serverProcess.on('close', (code) => {
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

        // Health polling loop
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
        const response = await axios.get(`${API_BASE}/status`, { timeout: 2000 });
        const status = response.data.status;
        const currentModel = response.data.current_model || 'Supervisor';
        const modelShort = currentModel.split('\\').pop()?.split('/').pop()?.replace('.gguf', '') || currentModel;

        if (status === 'online') {
            statusBarItem.text = `$(check) Serenity: ${modelShort}`;
            statusBarItem.tooltip = `SerenityDev Server Online (${currentModel})\nClick for Control Panel`;
            statusBarItem.backgroundColor = undefined;
        } else if (status === 'paused') {
            statusBarItem.text = `$(pause) Serenity: Paused`;
            statusBarItem.tooltip = `SerenityDev Server Paused\nClick to Resume`;
            statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
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
    context.subscriptions.push(serverOutputChannel);

    // Initialize Status Bar
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.command = 'serenity.showMenu';
    context.subscriptions.push(statusBarItem);

    // Auto-start server in background on activation
    ensureServerStarted(context).then(online => {
        if (online) {
            axios.get(`${API_BASE}/config`).then(res => {
                const model = res.data?.current_model || 'Supervisor';
                vscode.window.showInformationMessage(`🟢 SerenityDev Server Online: ${model}`);
            }).catch(() => {});
        }
    });

    // Register Native VS Code Chat Participant
    const chatParticipant = vscode.chat.createChatParticipant('serenitydev.assistant', async (request: vscode.ChatRequest, chatContext: vscode.ChatContext, response: vscode.ChatResponseStream, token: vscode.CancellationToken) => {
        response.progress('Initializing SerenityDev routing pipeline...');

        return new Promise<void>((resolve) => {
            let fullPrompt = request.prompt;
            if (request.command) {
                fullPrompt = `/${request.command} ${fullPrompt}`;
            }

            const postData = JSON.stringify({
                prompt: fullPrompt,
                session_id: 'native_chat',
                workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
            });

            const req = http.request({
                hostname: 'localhost',
                port: 8002,
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

                    for (let line of lines) {
                        line = line.trim();
                        if (line.startsWith('data:')) {
                            try {
                                const jsonStr = line.substring(5).trim();
                                const data = JSON.parse(jsonStr);
                                if (data.type === 'progress' && data.text) {
                                    response.progress(data.text);
                                } else if (data.type === 'thought' && data.content) {
                                    response.progress(`💭 Reasoning: ${data.content.trim().slice(0, 80)}...`);
                                } else if (data.type === 'content' && data.content) {
                                    response.markdown(createSafeMarkdown(data.content));
                                } else if (data.type === 'error' && data.detail) {
                                    response.markdown(createSafeMarkdown(`❌ **Error:** ${data.detail}`));
                                } else if (data.type === 'done' && data.routing) {
                                    const routing = data.routing;
                                    let routingInfo = `\n\n---\n\n> 🗺️ **Routing:** \`Supervisor\` ➡️ \`Worker: ${routing.worker}\`\n> 🔍 **Review:** \`${routing.review_badge}\``;
                                    if (routing.steps && routing.steps.length > 0) {
                                        routingInfo += `\n>\n> 🛠️ **Agentic Tools Executed:**\n`;
                                        routing.steps.forEach((s: any) => {
                                             const icon = s.status === 'success' ? '🟢' : '🟡';
                                             routingInfo += `> - ${icon} \`${s.tool}\` ➡️ *${s.details}*\n`;
                                         });
                                     }
                                     response.markdown(createSafeMarkdown(routingInfo));
                                 }
                            } catch (e) {
                                // ignore parse error
                            }
                        }
                    }
                });

                res.on('end', () => {
                    if (buffer.trim().startsWith('data:')) {
                        try {
                            const jsonStr = buffer.trim().substring(5).trim();
                            const data = JSON.parse(jsonStr);
                            if (data.type === 'content' && data.content) {
                                response.markdown(createSafeMarkdown(data.content));
                            }
                        } catch (e) { }
                    }
                    resolve();
                });
            });

            req.on('error', (err: any) => {
                response.markdown(createSafeMarkdown(`❌ **Error calling SerenityDev server:** ${err.message}`));
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

    // Register Native VS Code Language Model Provider (so models show up in picker)
    const lmProvider: vscode.LanguageModelChatProvider = {
        async provideLanguageModelChatInformation(options: any, token: vscode.CancellationToken): Promise<vscode.LanguageModelChatInformation[]> {
            try {
                const response = await axios.get(`${API_BASE}/models`, { timeout: 3000 });
                if (response.data && Array.isArray(response.data.models)) {
                    return response.data.models.map((m: any) => ({
                        id: m.id || 'serenity-supervisor',
                        name: m.name || m.id,
                        family: m.family || 'serenity-supervisor',
                        version: m.version || '1.0.0',
                        maxInputTokens: typeof m.maxInputTokens === 'number' ? m.maxInputTokens : 16384,
                        maxOutputTokens: typeof m.maxOutputTokens === 'number' ? m.maxOutputTokens : 16384,
                        capabilities: m.capabilities || { toolCalling: true, imageInput: false }
                    })) as vscode.LanguageModelChatInformation[];
                }
            } catch (err) {
                console.error("Failed to fetch models from devserver:", err);
            }
            return [
                {
                    id: "serenity-supervisor",
                    name: "SerenityDev Supervisor (Orchestrator)",
                    family: "serenity-supervisor",
                    version: "1.0.0",
                    maxInputTokens: 16384,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                }
            ];
        },

        async provideLanguageModelChatResponse(
            model: vscode.LanguageModelChatInformation,
            messages: readonly vscode.LanguageModelChatRequestMessage[],
            options: vscode.ProvideLanguageModelChatResponseOptions,
            progress: vscode.Progress<vscode.LanguageModelResponsePart>,
            token: vscode.CancellationToken
        ): Promise<void> {
            let prompt = '';
            messages.forEach(msg => {
                let textContent = '';
                msg.content.forEach(part => {
                    if (part instanceof vscode.LanguageModelTextPart) {
                        textContent += part.value;
                    }
                });

                if (msg.role === vscode.LanguageModelChatMessageRole.User) {
                    prompt += `User: ${textContent}\n`;
                } else if (msg.role === vscode.LanguageModelChatMessageRole.Assistant) {
                    prompt += `Assistant: ${textContent}\n`;
                } else {
                    prompt += `System: ${textContent}\n`;
                }
            });

            return new Promise<void>((resolve) => {
                const postData = JSON.stringify({
                    prompt: prompt,
                    model: model.id,
                    session_id: 'native_lm_picker',
                    workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
                });

                const req = http.request({
                    hostname: 'localhost',
                    port: 8002,
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

                        for (let line of lines) {
                            line = line.trim();
                            if (line.startsWith('data:')) {
                                try {
                                    const jsonStr = line.substring(5).trim();
                                    const data = JSON.parse(jsonStr);
                                    if (data.type === 'content' && data.content) {
                                        progress.report(new vscode.LanguageModelTextPart(data.content));
                                    } else if (data.type === 'error' && data.detail) {
                                        progress.report(new vscode.LanguageModelTextPart(`❌ **Error:** ${data.detail}`));
                                    }
                                } catch (e) {
                                    // ignore JSON errors
                                }
                            }
                        }
                    });

                    res.on('end', () => {
                        if (buffer.trim().startsWith('data:')) {
                            try {
                                const jsonStr = buffer.trim().substring(5).trim();
                                const data = JSON.parse(jsonStr);
                                if (data.type === 'content' && data.content) {
                                    progress.report(new vscode.LanguageModelTextPart(data.content));
                                }
                            } catch (e) { }
                        }
                        resolve();
                    });
                });

                req.on('error', (err: any) => {
                    progress.report(new vscode.LanguageModelTextPart(`❌ **Error calling SerenityDev server:** ${err.message}`));
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
            const content = typeof text === 'string' ? text : text.content.map(p => {
                if (p instanceof vscode.LanguageModelTextPart) {
                    return p.value;
                }
                return '';
            }).join('');
            return Math.ceil(content.length / 4);
        }
    };

    context.subscriptions.push(
        vscode.lm.registerLanguageModelChatProvider('serenitydev', lmProvider)
    );

    // Register Sidebar Chat Webview
    const provider = new SerenityChatProvider(context.extensionUri, context);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(SerenityChatProvider.viewType, provider)
    );

    // Command: Start Server
    const startServerDisposable = vscode.commands.registerCommand('serenity.startServer', async () => {
        vscode.window.showInformationMessage('🚀 Starting SerenityDev Server...');
        const online = await ensureServerStarted(context, true);
        if (online) {
            vscode.window.showInformationMessage('🟢 SerenityDev Server is running and healthy.');
        } else {
            vscode.window.showErrorMessage('❌ Failed to start SerenityDev Server. Check output logs.');
        }
        await updateStatusBar();
    });
    context.subscriptions.push(startServerDisposable);

    // Command: Select Active Consolidated Model
    const selectModelDisposable = vscode.commands.registerCommand('serenity.selectModel', async () => {
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
    });
    context.subscriptions.push(selectModelDisposable);

    // Command: Scan & Detect Models
    const scanModelsDisposable = vscode.commands.registerCommand('serenity.scanModels', async () => {
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
    });
    context.subscriptions.push(scanModelsDisposable);

    // Command: Explain Planning & Orchestration Library
    const explainPlanningDisposable = vscode.commands.registerCommand('serenity.explainPlanning', () => {
        const panel = vscode.window.createWebviewPanel(
            'serenityPlanningExplainer',
            'SerenityDev: Planning & Orchestration Architecture',
            vscode.ViewColumn.Beside,
            { enableScripts: true, retainContextWhenHidden: true }
        );
        panel.webview.html = getPlanningExplainerHtml();
    });
    context.subscriptions.push(explainPlanningDisposable);

    // Command: Restart Server
    const restartDisposable = vscode.commands.registerCommand('serenity.restartServer', async () => {
        try {
            await axios.post(`${API_BASE}/restart`);
            vscode.window.showInformationMessage('🔄 SerenityDev Server restarted. Internal state reset.');
            await updateStatusBar();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to restart SerenityDev server: ${err.message}`);
        }
    });
    context.subscriptions.push(restartDisposable);

    // Command: Unload Model
    const unloadDisposable = vscode.commands.registerCommand('serenity.unloadModel', async () => {
        try {
            await axios.post(`${API_BASE}/control/unload`);
            vscode.window.showInformationMessage('🧹 SerenityDev: Active models unloaded. VRAM freed.');
            await updateStatusBar();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to unload models: ${err.message}`);
        }
    });
    context.subscriptions.push(unloadDisposable);

    // Command: Set K/V Cache Quantization
    const setKVCacheDisposable = vscode.commands.registerCommand('serenity.setKVCache', async () => {
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
    });
    context.subscriptions.push(setKVCacheDisposable);

    // Command: Set Context Size
    const setContextSizeDisposable = vscode.commands.registerCommand('serenity.setContextSize', async () => {
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
    });
    context.subscriptions.push(setContextSizeDisposable);

    // Command: Set GPU Layer Offload Count
    const setGpuLayersDisposable = vscode.commands.registerCommand('serenity.setGpuLayers', async () => {
        const layerChoice = await vscode.window.showQuickPick([
            'Auto (Dynamic Shared VRAM Guard)',
            '0 (CPU Only)',
            '4 Layers',
            '16 Layers',
            '32 Layers',
            '40 Layers',
            '60 Layers',
            '99 (All Layers)',
            'Custom Value...'
        ], { placeHolder: 'Select GPU Layer Offload Count' });
        if (!layerChoice) { return; }

        let layers = -1;
        if (layerChoice.startsWith('Auto')) {
            layers = -1;
        } else if (layerChoice.startsWith('Custom')) {
            const input = await vscode.window.showInputBox({
                prompt: 'Enter number of GPU layers to offload (-1 for auto/dynamic)',
                validateInput: (v) => (!isNaN(Number(v)) && Number(v) >= -1 ? null : 'Must be integer >= -1')
            });
            if (!input) { return; }
            layers = parseInt(input, 10);
        } else {
            layers = parseInt(layerChoice.split(' ')[0], 10);
        }

        try {
            await axios.post(`${API_BASE}/config`, { gpu_layers: layers });
            const label = layers === -1 ? 'Auto (Dynamic VRAM Guard)' : `${layers} layers`;
            vscode.window.showInformationMessage(`⚡ GPU Layer Offload set to: ${label}`);
            await updateStatusBar();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to update GPU layers: ${err.message}`);
        }
    });
    context.subscriptions.push(setGpuLayersDisposable);

    // Command: Assign Model to Role/Effort Level
    const setRoleModelDisposable = vscode.commands.registerCommand('serenity.setRoleModel', async () => {
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
    });
    context.subscriptions.push(setRoleModelDisposable);

    // Command: Toggle Auto-Continue (Unlimited Iteration)
    const toggleAutoContinueDisposable = vscode.commands.registerCommand('serenity.toggleAutoContinue', async () => {
        try {
            const cfgRes = await axios.get(`${API_BASE}/config`);
            const currentAuto = cfgRes.data?.auto_continue || false;
            const newAuto = !currentAuto;
            await axios.post(`${API_BASE}/config`, { auto_continue: newAuto });
            const statusStr = newAuto ? 'ENABLED (Unlimited Iteration up to 500 loops)' : 'DISABLED (Standard step caps)';
            vscode.window.showInformationMessage(`♾️ Auto-Continue ${statusStr}`);
            await updateStatusBar();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to toggle Auto-Continue: ${err.message}`);
        }
    });
    context.subscriptions.push(toggleAutoContinueDisposable);

    // Helper to prompt for adding a model folder
    async function promptAddModelFolder() {
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
    }

    let addFolderDisposable = vscode.commands.registerCommand('serenity.addModelFolder', promptAddModelFolder);
    context.subscriptions.push(addFolderDisposable);

    // Register Server Control Quickpick Menu
    let menuDisposable = vscode.commands.registerCommand('serenity.showMenu', async () => {
        const options = [
            '🚀 Start SerenityDev Server',
            '🎯 Select Active Consolidated Model...',
            '🔍 Scan & Detect Local GGUF Models',
            '🎭 Assign Models to Roles / Effort Levels...',
            '♾️ Toggle Auto-Continue (Unlimited Iteration)',
            'ℹ️ Explain Planning & Orchestration Library',
            '🔄 Restart Server',
            '🧹 Unload Model (Free VRAM)',
            '⚙️ Set K/V Cache Quantization...',
            '📐 Set Context Window Size...',
            '⚡ Set GPU Layer Offload...',
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
        } else if (selection.includes('Select Active Consolidated Model')) {
            vscode.commands.executeCommand('serenity.selectModel');
        } else if (selection.includes('Scan & Detect')) {
            vscode.commands.executeCommand('serenity.scanModels');
        } else if (selection.includes('Explain Planning')) {
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
            await promptAddModelFolder();
        }
    });

    const interval = setInterval(updateStatusBar, 5000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
    context.subscriptions.push(menuDisposable);
    updateStatusBar();
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

        // Handle incoming messages from Webview Interface
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

                    // Send query out to local server harness
                    const response = await axios.post(ASK_URL, {
                        prompt: data.value,
                        session_id: this._sessionId,
                        workspace_dir: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || ''
                    }, { timeout: 180000 }); // 3 minute timeout for multi-step agent

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
            } else if (data.type === 'revertEdit') {
                try {
                    await axios.post(`${API_BASE}/edit/revert`, { backup_id: data.backupId });
                    vscode.window.showInformationMessage(`Reverted file edit (${data.backupId})`);
                } catch (e: any) {
                    vscode.window.showErrorMessage(`Failed to revert edit: ${e.message}`);
                }
            } else if (data.type === 'keepEdit') {
                try {
                    await axios.post(`${API_BASE}/edit/keep`, { backup_id: data.backupId });
                } catch (e) { }
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
                    this._view?.webview.postMessage({
                        type: 'configLoaded',
                        config: cfgRes.data
                    });
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to switch active model: ${err.message}`);
                }
            } else if (data.type === 'updateRoleAssignment') {
                try {
                    await axios.post(`${API_BASE}/config`, {
                        roles: { [data.role]: data.model }
                    });
                    vscode.window.showInformationMessage(`🎭 Updated role '${data.role}' to model: ${data.model}`);
                } catch (err: any) {
                    vscode.window.showErrorMessage(`Failed to update role model: ${err.message}`);
                }
            } else if (data.type === 'startServer') {
                vscode.commands.executeCommand('serenity.startServer');
            } else if (data.type === 'scanModels') {
                vscode.commands.executeCommand('serenity.scanModels');
            } else if (data.type === 'toggleAutoContinue') {
                vscode.commands.executeCommand('serenity.toggleAutoContinue');
            } else if (data.type === 'runCommand') {
                if (data.command === 'explainPlanning') {
                    vscode.commands.executeCommand('serenity.explainPlanning');
                } else if (data.command === 'selectModel') {
                    vscode.commands.executeCommand('serenity.selectModel');
                } else if (data.command === 'scanModels') {
                    vscode.commands.executeCommand('serenity.scanModels');
                } else if (data.command === 'setRoleModel') {
                    vscode.commands.executeCommand('serenity.setRoleModel');
                } else if (data.command === 'toggleAutoContinue') {
                    vscode.commands.executeCommand('serenity.toggleAutoContinue');
                } else if (data.command === 'restartServer') {
                    vscode.commands.executeCommand('serenity.restartServer');
                } else if (data.command === 'unloadModel') {
                    vscode.commands.executeCommand('serenity.unloadModel');
                } else if (data.command === 'setKVCache') {
                    vscode.commands.executeCommand('serenity.setKVCache');
                } else if (data.command === 'setContextSize') {
                    vscode.commands.executeCommand('serenity.setContextSize');
                } else if (data.command === 'setGpuLayers') {
                    vscode.commands.executeCommand('serenity.setGpuLayers');
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
                body { font-family: var(--vscode-font-family); color: var(--vscode-editor-foreground); padding: 8px; font-size: 12px; display: flex; flex-direction: column; height: 100vh; margin: 0; box-sizing: border-box; }
                #chatContainer { display: flex; flex-direction: column; height: 98vh; width: 100%; }
                
                .header-bar { display: flex; justify-content: space-between; align-items: center; padding-bottom: 6px; border-bottom: 1px solid var(--vscode-widget-border); margin-bottom: 6px; }
                .status-badge { font-size: 10px; font-weight: bold; padding: 2px 6px; border-radius: 4px; display: inline-flex; align-items: center; gap: 4px; }
                .status-online { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }
                .status-offline { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); cursor: pointer; }
                
                .model-bar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; font-size: 11px; }
                .active-model-select { flex: 1; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 4px; padding: 3px 6px; font-size: 11px; font-weight: 500; }
                
                .toolbar { display: flex; flex-wrap: wrap; gap: 4px; padding-bottom: 6px; border-bottom: 1px solid var(--vscode-widget-border); margin-bottom: 6px; }
                .tool-btn { background: var(--vscode-editor-inactiveSelectionBackground); color: var(--vscode-foreground); border: 1px solid var(--vscode-widget-border); font-size: 10px; padding: 2px 6px; border-radius: 4px; cursor: pointer; transition: background 0.2s; }
                .tool-btn:hover { background: var(--vscode-button-hoverBackground); color: var(--vscode-button-foreground); }
                .tool-btn.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); font-weight: bold; }
                .tool-btn.accent { background: #9333ea; color: white; font-weight: bold; }
                
                #output { flex: 1; overflow-y: auto; padding: 4px; border-bottom: 1px solid var(--vscode-widget-border); margin-bottom: 8px; }
                .msg { margin-bottom: 10px; padding: 6px 10px; border-radius: 6px; line-height: 1.4; }
                .user { background: var(--vscode-button-background); color: var(--vscode-button-foreground); align-self: flex-end; }
                .agent { background: var(--vscode-editor-inactiveSelectionBackground); }
                
                #inputContainer { display: flex; gap: 4px; }
                textarea { flex: 1; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); resize: none; border-radius: 4px; font-family: inherit; font-size: 12px; padding: 5px; }
                button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-weight: bold; }
                button:hover { background: var(--vscode-button-hoverBackground); }
                .routing-box { font-family: var(--vscode-editor-font-family); font-size: 10px; background: rgba(0,0,0,0.2); border-left: 2px solid var(--vscode-textLink-foreground); padding: 4px; margin-top: 4px; border-radius: 2px; }
                
                .offline-banner { display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 8px; margin-bottom: 8px; text-align: center; }
                .offline-banner button { background: #dc2626; color: white; margin-top: 4px; padding: 4px 12px; border-radius: 4px; font-size: 11px; cursor: pointer; }
                
                /* Modal & Drawer Dialogs */
                .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 100; padding: 12px; box-sizing: border-box; }
                .modal-card { background: var(--vscode-editor-background); border: 1px solid var(--vscode-widget-border); border-radius: 8px; padding: 14px; max-height: 90vh; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }
                .modal-title { font-weight: bold; font-size: 13px; color: var(--vscode-textLink-foreground); display: flex; justify-content: space-between; align-items: center; }
                .modal-close { background: transparent; border: none; color: var(--vscode-foreground); cursor: pointer; font-size: 14px; }
                .modal-section { background: var(--vscode-editor-inactiveSelectionBackground); padding: 8px; border-radius: 6px; font-size: 11px; margin-bottom: 4px; }
                .modal-section h4 { margin: 0 0 4px 0; color: var(--vscode-textLink-activeForeground); }
                
                /* Role Matrix Form */
                .role-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 11px; }
                .role-select { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 4px; padding: 2px 4px; font-size: 11px; max-width: 55%; }
            </style>
        </head>
        <body>
            <div id="chatContainer">
                <div class="header-bar">
                    <span style="font-weight: bold; font-size: 11px;">🧠 SerenityDev Core</span>
                    <span id="serverStatusBadge" class="status-badge status-offline" onclick="sendCmd('startServer')">🔴 Offline (Click to Start)</span>
                </div>

                <div id="offlineBanner" class="offline-banner">
                    <div>⚠️ Server is offline on port 8002</div>
                    <button onclick="sendCmd('startServer')">🚀 Start Serenity Server</button>
                </div>

                <div class="model-bar">
                    <span style="opacity: 0.8; font-size: 10px;">Model:</span>
                    <select id="activeModelSelect" class="active-model-select" onchange="onModelDropdownChange(this)">
                        <option value="">Loading detected models...</option>
                    </select>
                </div>

                <div class="toolbar">
                    <button class="tool-btn primary" onclick="showExplainModal()">ℹ️ Explain</button>
                    <button class="tool-btn" onclick="sendCmd('scanModels')">🔍 Scan</button>
                    <button class="tool-btn accent" onclick="showRolesModal()">🎭 Roles</button>
                    <button class="tool-btn" onclick="sendCmd('toggleAutoContinue')">♾️ Auto-Cont</button>
                    <button class="tool-btn" onclick="sendCmd('restartServer')">🔄 Restart</button>
                    <button class="tool-btn" onclick="sendCmd('unloadModel')">🧹 Unload</button>
                    <button class="tool-btn" onclick="sendCmd('setKVCache')">⚙️ KV</button>
                    <button class="tool-btn" onclick="sendCmd('setContextSize')">📐 Ctx</button>
                    <button class="tool-btn" onclick="sendCmd('setGpuLayers')">⚡ GPU</button>
                </div>
                
                <div id="output">
                    <div class="msg agent">
                        <strong>SerenityDev:</strong> Planning & Orchestration Library Initialized.<br/>
                        <span style="opacity: 0.8; font-size: 10px;">Select a model or type queries below to execute multi-agent plans.</span>
                    </div>
                </div>
                
                <div id="inputContainer">
                    <textarea id="promptInput" rows="2" placeholder="Ask Serenity to plan or edit code..."></textarea>
                    <button id="sendBtn" onclick="submitQuery()">Ask</button>
                    <button id="clearBtn" onclick="clearSession()" style="background: var(--vscode-errorForeground);">Clear</button>
                </div>
            </div>

            <!-- Explainer Modal Dialog -->
            <div id="explainModal" class="modal-overlay">
                <div class="modal-card">
                    <div class="modal-title">
                        <span>🧠 Planning & Orchestration Library</span>
                        <button class="modal-close" onclick="hideExplainModal()">✕</button>
                    </div>
                    
                    <div class="modal-section">
                        <h4>1. Hierarchical Supervisor-Worker Effort Tiers</h4>
                        • <strong>Supervisor (Low Effort):</strong> 8 max steps, rapid token savings.<br/>
                        • <strong>Supervisor (High Effort):</strong> 25 max steps, full architectural reasoning.<br/>
                        • <strong>Orchestrator (Turbo Effort):</strong> 100 max steps, autonomous multi-turn loop.<br/>
                        • <strong>Auto-Continue:</strong> Enables up to 500 loops for indefinite task completion.
                    </div>
                    
                    <div class="modal-section">
                        <h4>2. Model Selection & Detection</h4>
                        All GGUF models in <code>models/</code> and custom configured paths (e.g. <code>S:\LLM</code>) are dynamically discovered and available in the top dropdown selector.
                    </div>

                    <div class="modal-section">
                        <h4>3. Dynamic Memory & Hardware Governance</h4>
                        • <strong>Unload Model:</strong> Releases all GPU VRAM allocations immediately.<br/>
                        • <strong>K/V Cache:</strong> Select quantization (f16, q8_0, q4_0, q5_1) for memory savings.<br/>
                        • <strong>Context Size:</strong> 2k to 256k tokens.<br/>
                        • <strong>GPU Layers:</strong> Dynamic Shared VRAM Guard or explicit layer offloading.
                    </div>

                    <div style="display: flex; justify-content: flex-end; gap: 6px; margin-top: 4px;">
                        <button onclick="sendCmd('explainPlanning'); hideExplainModal();" style="font-size: 10px; padding: 4px 8px;">Open Full Page Guide</button>
                        <button onclick="hideExplainModal()" style="font-size: 10px; padding: 4px 8px; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground);">Close</button>
                    </div>
                </div>
            </div>

            <!-- Roles Configuration Modal Dialog -->
            <div id="rolesModal" class="modal-overlay">
                <div class="modal-card">
                    <div class="modal-title">
                        <span>🎭 Role & Effort Model Assignment</span>
                        <button class="modal-close" onclick="hideRolesModal()">✕</button>
                    </div>
                    <p style="font-size: 10px; opacity: 0.8; margin: 0 0 6px 0;">Assign specific local models per effort tier and worker specialization:</p>
                    
                    <div id="rolesContainer" class="modal-section">
                        <em>Loading role configurations...</em>
                    </div>

                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 4px;">
                        <button onclick="sendCmd('setRoleModel'); hideRolesModal();" style="font-size: 10px; padding: 3px 6px;">Pick via Command Palette</button>
                        <button onclick="hideRolesModal()" style="font-size: 10px; padding: 3px 8px; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground);">Done</button>
                    </div>
                </div>
            </div>

            <script>
                const vscode = acquireVsCodeApi();
                let globalConfig = null;

                function escapeHtml(str) {
                    if (!str) return '';
                    return String(str)
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#039;');
                }

                function showExplainModal() {
                    document.getElementById('explainModal').style.display = 'block';
                }

                function hideExplainModal() {
                    document.getElementById('explainModal').style.display = 'none';
                }

                function showRolesModal() {
                    document.getElementById('rolesModal').style.display = 'block';
                    vscode.postMessage({ type: 'fetchConfig' });
                }

                function hideRolesModal() {
                    document.getElementById('rolesModal').style.display = 'none';
                }

                function sendCmd(commandName) {
                    vscode.postMessage({ type: 'runCommand', command: commandName });
                }

                function onModelDropdownChange(selectEl) {
                    const chosen = selectEl.value;
                    if (chosen) {
                        vscode.postMessage({ type: 'selectActiveModel', model: chosen });
                    }
                }

                function updateRoleSelect(roleKey, selectEl) {
                    const chosen = selectEl.value;
                    vscode.postMessage({
                        type: 'updateRoleAssignment',
                        role: roleKey,
                        model: chosen
                    });
                }
                
                function submitQuery() {
                    const input = document.getElementById('promptInput');
                    const text = input.value.trim();
                    if(!text) return;

                    // Append user text safely escaped
                    const out = document.getElementById('output');
                    out.innerHTML += '<div class="msg user"><strong>You:</strong> ' + escapeHtml(text) + '</div>';
                    
                    // Add status indicator
                    out.innerHTML += '<div id="statusBox" class="routing-box">⚙️ Initializing Routing Pipeline...</div>';
                    
                    vscode.postMessage({ type: 'sendQuery', value: text });
                    input.value = '';
                    out.scrollTop = out.scrollHeight;
                }

                function clearSession() {
                    const out = document.getElementById('output');
                    out.innerHTML = '<div class="msg agent"><strong>SerenityDev:</strong> Session cleared. New context initialized.</div>';
                    vscode.postMessage({ type: 'clearSession' });
                }

                function keepEdit(backupId, btnEl) {
                    vscode.postMessage({ type: 'keepEdit', backupId: backupId });
                    if (btnEl && btnEl.parentElement) {
                        btnEl.parentElement.innerHTML = '<span style="color: #4ec9b0; font-weight: bold;">✓ Kept</span>';
                    }
                }
                function rejectEdit(backupId, btnEl) {
                    vscode.postMessage({ type: 'revertEdit', backupId: backupId });
                    if (btnEl && btnEl.parentElement) {
                        btnEl.parentElement.innerHTML = '<span style="color: #f14c4c; font-weight: bold;">❌ Reverted</span>';
                    }
                }
                function parseProofBadges(rawText) {
                    if (!rawText) return '';
                    return rawText.replace(/(?:PROOF:\s*)?(edited:[^\s\n\(\)]+)-(\d+)\+(\d+)(?:\s*\(backup:([^\)]+)\))?/g, function(match, proof, dels, adds, bakId) {
                        const bId = bakId ? bakId.trim() : '';
                        const safeBId = escapeHtml(bId);
                        const safeProof = escapeHtml(proof);
                        const safeDels = escapeHtml(dels);
                        const safeAdds = escapeHtml(adds);
                        const btnHtml = safeBId ? '<span style="margin-left: 8px;">' +
                            '<button onclick="keepEdit(\'' + safeBId + '\', this)" style="background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; margin-right: 4px; font-weight: bold; font-size: 10px;">Keep</button>' +
                            '<button onclick="rejectEdit(\'' + safeBId + '\', this)" style="background: var(--vscode-errorForeground); color: white; border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-weight: bold; font-size: 10px;">Reject</button>' +
                            '</span>' : '';
                        return '<div style="background: rgba(0,0,0,0.3); border: 1px solid var(--vscode-widget-border); padding: 6px 10px; border-radius: 6px; margin: 4px 0; font-family: var(--vscode-editor-font-family); font-size: 11px; display: flex; justify-content: space-between; align-items: center;">' +
                               '<span>📝 <code>' + safeProof + '-' + safeDels + '+' + safeAdds + '</code></span>' + btnHtml + '</div>';
                    });
                }

                // Initial config fetch
                vscode.postMessage({ type: 'fetchConfig' });
                setInterval(() => {
                    vscode.postMessage({ type: 'fetchConfig' });
                }, 8000);

                window.addEventListener('message', event => {
                    const message = event.data;
                    const out = document.getElementById('output');
                    
                    if (message.type === 'serverOffline') {
                        const badge = document.getElementById('serverStatusBadge');
                        const banner = document.getElementById('offlineBanner');
                        if (badge) {
                            badge.className = 'status-badge status-offline';
                            badge.textContent = '🔴 Offline (Click to Start)';
                        }
                        if (banner) { banner.style.display = 'block'; }
                        return;
                    } else if (message.type === 'configLoaded') {
                        globalConfig = message.config;
                        const badge = document.getElementById('serverStatusBadge');
                        const banner = document.getElementById('offlineBanner');
                        const modelSelect = document.getElementById('activeModelSelect');

                        if (badge) {
                            badge.className = 'status-badge status-online';
                            badge.textContent = '🟢 Online';
                        }
                        if (banner) { banner.style.display = 'none'; }

                        if (modelSelect && globalConfig) {
                            const available = globalConfig.available_models || [globalConfig.current_model];
                            const current = globalConfig.current_model;
                            let html = '';
                            available.forEach(m => {
                                const sel = (m === current || current.startsWith(m) || m.startsWith(current)) ? 'selected' : '';
                                html += '<option value="' + escapeHtml(m) + '" ' + sel + '>' + escapeHtml(m) + '</option>';
                            });
                            modelSelect.innerHTML = html;
                        }

                        const container = document.getElementById('rolesContainer');
                        if (container && globalConfig) {
                            const roles = [
                                { label: '👑 Supervisor (Low)', key: 'supervisor_low' },
                                { label: '👑 Supervisor (High)', key: 'supervisor_high' },
                                { label: '⚡ Orchestrator (Turbo)', key: 'orchestrator_turbo' },
                                { label: '🛠️ W1 (Architecture)', key: 'w1_reasoning' },
                                { label: '💻 W2 (Code Synthesis)', key: 'w2_code' },
                                { label: '⚡ W3 (Fast Utilities)', key: 'w3_fast' },
                                { label: '🧩 W4 (Specialized)', key: 'w4_specialized' },
                                { label: '✍️ FIM (Autocomplete)', key: 'fim' }
                            ];
                            const available = globalConfig.available_models || [globalConfig.current_model];
                            const currentRoles = globalConfig.roles || {};

                            let rHtml = '';
                            roles.forEach(r => {
                                const currentVal = currentRoles[r.key] || globalConfig.current_model || '';
                                let optHtml = '';
                                available.forEach(m => {
                                    const sel = (m === currentVal || currentVal.startsWith(m) || m.startsWith(currentVal)) ? 'selected' : '';
                                    optHtml += '<option value="' + escapeHtml(m) + '" ' + sel + '>' + escapeHtml(m) + '</option>';
                                });
                                rHtml += '<div class="role-row"><span>' + escapeHtml(r.label) + '</span><select class="role-select" onchange="updateRoleSelect(\'' + r.key + '\', this)">' + optHtml + '</select></div>';
                            });
                            container.innerHTML = rHtml;
                        }
                        return;
                    } else if (message.type === 'updateStatus') {
                        const statusBox = document.getElementById('statusBox');
                        if (statusBox) {
                            let logHtml = '';
                            message.logs.forEach(l => { logHtml += '<div>⚙️ ' + escapeHtml(l) + '</div>'; });
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
                            if (message.routing.steps && message.routing.steps.length > 0) {
                                message.routing.steps.forEach(s => {
                                    stepList += '<div style="margin-top: 2px;">🛠️ Step ' + escapeHtml(String(s.step)) + ': <code>' + escapeHtml(String(s.tool)) + '</code> ➡️ ' + parseProofBadges(s.details) + '</div>';
                                });
                            }
                            routingHtml = '<div class="routing-box">🗺️ Plan Step Count: ' + (message.routing.steps ? message.routing.steps.length : 0) + '<br/>Worker: ' + escapeHtml(String(message.routing.worker)) + stepList + '</div>';
                        }
                        const formattedVal = parseProofBadges(message.value);
                        out.innerHTML += '<div class="msg agent"><strong>SerenityDev:</strong> ' + formattedVal + routingHtml + '</div>';
                    } else if (message.type === 'addError') {
                        out.innerHTML += '<div class="msg" style="color: var(--vscode-errorForeground)">❌ Error: ' + escapeHtml(String(message.value)) + '</div>';
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
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: #0d1117;
                color: #c9d1d9;
                padding: 24px;
                line-height: 1.6;
                max-width: 900px;
                margin: 0 auto;
            }
            h1, h2, h3 { color: #58a6ff; font-weight: 600; }
            h1 { border-bottom: 1px solid #30363d; padding-bottom: 10px; font-size: 24px; }
            h2 { font-size: 18px; margin-top: 24px; color: #79c0ff; }
            .card {
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 16px;
                margin: 12px 0;
            }
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 12px;
                margin: 12px 0;
            }
            .badge {
                display: inline-block;
                background: #238636;
                color: #ffffff;
                padding: 2px 8px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 600;
                margin-right: 6px;
            }
            .code-box {
                background: #090d13;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 10px;
                font-family: monospace;
                font-size: 12px;
                color: #e6edf3;
                overflow-x: auto;
            }
            ul { padding-left: 20px; }
            li { margin-bottom: 6px; }
        </style>
    </head>
    <body>
        <h1>🧠 SerenityDev: Planning & Orchestration Architecture</h1>
        <p>
            The <strong>Planning & Orchestration Library</strong> is SerenityDev's hierarchical multi-agent coordination core.
            It provides autonomous agent planning, role-based effort level model assignment, multi-turn tool calling, and active memory governance directly on local hardware.
        </p>

        <h2>🗺️ 1. Hierarchical Supervisor-Worker Effort Tiers</h2>
        <div class="grid">
            <div class="card">
                <h3>👑 Supervisor (Low Effort)</h3>
                <p>Fast, token-efficient routing and planning capped at 8 max steps. Ideal for simple queries and low latency.</p>
            </div>
            <div class="card">
                <h3>👑 Supervisor (High Effort)</h3>
                <p>Deep reasoning and comprehensive plan formulation capped at 25 max steps with quality reviews.</p>
            </div>
            <div class="card">
                <h3>⚡ Orchestrator (Turbo Effort)</h3>
                <p>Autonomous multi-agent execution loop with 100 max steps for indefinite subagent delegation.</p>
            </div>
            <div class="card">
                <h3>♾️ Auto-Continue</h3>
                <p>Unlimited iteration mode allowing the orchestrator to loop up to 500 steps until the task is completely finished.</p>
            </div>
        </div>

        <h2>🛠️ 2. Specialized Worker Roles</h2>
        <div class="card">
            <p>Each worker tier can be assigned a dedicated GGUF model:</p>
            <ul>
                <li><strong>W1 (Architecture & Reasoning):</strong> Deep multi-step system design & problem decomposition</li>
                <li><strong>W2 (Heavy Code Synthesis):</strong> High-precision coding and AST-safe string replacements</li>
                <li><strong>W3 (Fast Utilities & Scripts):</strong> Rapid workspace discovery and utility tasks</li>
                <li><strong>W4 (Specialized Worker):</strong> Auxiliary tasks and multi-agent coordination</li>
                <li><strong>FIM (Inline Autocomplete):</strong> High-speed fill-in-the-middle completion</li>
            </ul>
        </div>

        <h2>🛡️ 3. Hardware & Memory Governance</h2>
        <div class="grid">
            <div class="card">
                <h3>🔄 Server Lifecycle</h3>
                <p>Soft restart clears memory queues and active locks without process termination.</p>
            </div>
            <div class="card">
                <h3>🧹 VRAM Unload</h3>
                <p>Releases all loaded models and GPU allocations immediately.</p>
            </div>
            <div class="card">
                <h3>⚙️ KV Cache Quantization</h3>
                <p>Dynamically switches K and V cache types (f16, q8_0, q4_0, q5_1) to drastically reduce memory usage.</p>
            </div>
            <div class="card">
                <h3>📐 Context Window (ctx)</h3>
                <p>Adjusts token capacity from 2,048 up to 128,000 tokens based on model capabilities.</p>
            </div>
            <div class="card">
                <h3>⚡ GPU Layer Offload</h3>
                <p>Explicit layer offload or automatic Dynamic Shared VRAM Guard to prevent Windows memory thrashing.</p>
            </div>
        </div>

        <h2>⌨️ Quick Commands in VS Code</h2>
        <div class="code-box">
Ctrl+Shift+P -> "Serenity: Start Server"<br/>
Ctrl+Shift+P -> "Serenity: Select Active Model"<br/>
Ctrl+Shift+P -> "Serenity: Scan & Detect Models"<br/>
Ctrl+Shift+P -> "Serenity: Open Server Status Control Panel"<br/>
Ctrl+Shift+P -> "Serenity: Assign Model to Role/Effort Level"<br/>
Ctrl+Shift+P -> "Serenity: Toggle Auto-Continue (Unlimited Iteration)"<br/>
Ctrl+Shift+P -> "Serenity: Explain Planning & Orchestration Library"<br/>
Ctrl+Shift+P -> "Serenity: Restart Server"<br/>
Ctrl+Shift+P -> "Serenity: Unload Model (Free VRAM)"<br/>
Ctrl+Shift+P -> "Serenity: Set K/V Cache Quantization Size"<br/>
Ctrl+Shift+P -> "Serenity: Set Context Window (ctx size)"<br/>
Ctrl+Shift+P -> "Serenity: Set GPU Layer Offload Count"
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