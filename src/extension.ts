import * as vscode from 'vscode';
import axios from 'axios';
import * as http from 'http';
import * as cp from 'child_process';
import * as path from 'path';
import { randomBytes } from 'crypto';

let serverProcess: cp.ChildProcess | undefined;
let serverOutputChannel: vscode.OutputChannel;

const API_BASE = 'http://localhost:8002/api';
const ASK_URL = 'http://localhost:8002/ask';

function createSafeMarkdown(content: string): vscode.MarkdownString {
    const md = new vscode.MarkdownString(content);
    md.isTrusted = false;
    md.supportHtml = false;
    return md;
}

export function activate(context: vscode.ExtensionContext) {
    serverOutputChannel = vscode.window.createOutputChannel("Serenity Server");
    context.subscriptions.push(serverOutputChannel);

    const config = vscode.workspace.getConfiguration('serenitydev');
    const pythonPath = config.get<string>('pythonPath') || 'python';
    const serverScriptPath = path.join(context.extensionPath, 'serenitydevserver.py');

    serverOutputChannel.appendLine(`Starting Serenity server using ${pythonPath} at ${serverScriptPath}...`);
    serverProcess = cp.spawn(pythonPath, [serverScriptPath], { cwd: context.extensionPath });

    if (serverProcess.stdout) {
        serverProcess.stdout.on('data', (data) => {
            serverOutputChannel.append(data.toString());
        });
    }

    if (serverProcess.stderr) {
        serverProcess.stderr.on('data', (data) => {
            serverOutputChannel.append(data.toString());
        });
    }

    serverProcess.on('close', (code) => {
        serverOutputChannel.appendLine(`Server process exited with code ${code}`);
    });

    let statusBarItem: vscode.StatusBarItem;

    // Initialize Status Bar
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.command = 'serenity.showMenu';
    context.subscriptions.push(statusBarItem);

    // Register Native VS Code Chat Participant
    const chatParticipant = vscode.chat.createChatParticipant('serenitydev.assistant', async (request: vscode.ChatRequest, context: vscode.ChatContext, response: vscode.ChatResponseStream, token: vscode.CancellationToken) => {
        response.progress('Initializing SerenityDev routing pipeline...');

        return new Promise<void>((resolve, reject) => {
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
                                // ignore
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

    // Helper to determine model capabilities dynamically based on name
    function getModelCapabilities(modelName: string): { toolCalling: boolean; imageInput: boolean } {
        const nameLower = modelName.toLowerCase();
        const imageInput = nameLower.includes('-vl') ||
            nameLower.includes('vision') ||
            nameLower.includes('llava') ||
            nameLower.includes('paligemma') ||
            nameLower.includes('minicpm-v');
        const isBaseOrFim = nameLower.includes('fim') ||
            nameLower.includes('-2b') ||
            nameLower.includes('base');
        const toolCalling = !isBaseOrFim;
        return { toolCalling, imageInput };
    }

    // Register Native VS Code Language Model Provider (so models show up in picker)
    const lmProvider: vscode.LanguageModelChatProvider = {
        async provideLanguageModelChatInformation(options: any, token: vscode.CancellationToken): Promise<vscode.LanguageModelChatInformation[]> {
            try {
                const response = await axios.get(`${API_BASE}/models`);
                if (response.data && response.data.models) {
                    return response.data.models as vscode.LanguageModelChatInformation[];
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
                }
            });

            return new Promise<void>((resolve, reject) => {
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

    let isPolling = false;
    async function updateStatusBar() {
        if (isPolling) { return; }
        isPolling = true;
        try {
            const response = await axios.get(`${API_BASE}/status`);
            const status = response.data.status;

            if (status === 'online') {
                statusBarItem.text = `$(check) Serenity: Online`;
                statusBarItem.backgroundColor = undefined;
            } else if (status === 'paused') {
                statusBarItem.text = `$(pause) Serenity: Paused`;
                statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
            }
        } catch (err) {
            statusBarItem.text = `$(error) Serenity: Offline`;
            statusBarItem.backgroundColor = undefined;
        } finally {
            isPolling = false;
        }
        statusBarItem.show();
    }

    // Register Sidebar Chat Webview
    const provider = new SerenityChatProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(SerenityChatProvider.viewType, provider)
    );

    // Register Server Control Quickpick Menu
    let disposable = vscode.commands.registerCommand('serenity.showMenu', async () => {
        const options = ['Resume Server', 'Pause Server', 'Restart Server'];
        const selection = await vscode.window.showQuickPick(options, {
            placeHolder: 'Serenity Server Control'
        });

        if (!selection) { return; }

        try {
            if (selection === 'Resume Server') {
                await axios.post(`${API_BASE}/control/resume`);
                vscode.window.showInformationMessage('Serenity Server Resumed');
            } else if (selection === 'Pause Server') {
                await axios.post(`${API_BASE}/control/pause`);
                vscode.window.showWarningMessage('Serenity Server Paused');
            } else if (selection === 'Restart Server') {
                await axios.post(`${API_BASE}/restart`);
                vscode.window.showWarningMessage('Restart signal sent.');
            }
            await updateStatusBar();
        } catch (error) {
            vscode.window.showErrorMessage('Failed to communicate with Serenity Server.');
        }
    });

    const interval = setInterval(updateStatusBar, 5000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
    context.subscriptions.push(disposable);
    updateStatusBar();
}

class SerenityChatProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'serenity.chatView';
    private _view?: vscode.WebviewView;
    private _sessionId: string;

    private _generateSessionId(): string {
        return `session_${randomBytes(16).toString('hex')}`;
    }

    constructor(private readonly _extensionUri: vscode.Uri) {
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
                            const statusRes = await axios.get(`${API_BASE}/status`);
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
            }
        });
    }


    private _getHtmlForWebview(webview: vscode.Webview) {
        return `<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <style>
                body { font-family: var(--vscode-font-family); color: var(--vscode-editor-foreground); padding: 10px; font-size: 12px; display: flex; flex-col; height: 100vh; margin: 0; box-sizing: border-box; }
                #chatContainer { display: flex; flex-direction: column; height: 95vh; width: 100%; }
                #output { flex: 1; overflow-y: auto; padding: 5px; border-bottom: 1px solid var(--vscode-widget-border); margin-bottom: 8px; }
                .msg { margin-bottom: 10px; padding: 6px 10px; rounded: 6px; border-radius: 6px; }
                .user { background: var(--vscode-button-background); color: var(--vscode-button-foreground); align-self: flex-end; }
                .agent { background: var(--vscode-editor-inactiveSelectionBackground); }
                #inputContainer { display: flex; gap: 4px; }
                textarea { flex: 1; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); resize: none; border-radius: 4px; font-family: inherit; font-size: 12px; padding: 5px; }
                button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-weight: bold; }
                button:hover { background: var(--vscode-button-hoverBackground); }
                .routing-box { font-family: var(--vscode-editor-font-family); font-size: 10px; background: rgba(0,0,0,0.2); border-left: 2px solid var(--vscode-textLink-foreground); padding: 4px; margin-top: 4px; }
            </style>
        </head>
        <body>
            <div id="chatContainer">
                <div id="output">
                    <div class="msg agent"><strong>SerenityDev:</strong> Gemma-4 Active Planning Pipeline Initialized. Ready for queries.</div>
                </div>
                <div id="inputContainer">
                    <textarea id="promptInput" rows="2" placeholder="Ask Serenity to plan or edit code..."></textarea>
                    <button id="sendBtn" onclick="submitQuery()">Ask</button>
                    <button id="clearBtn" onclick="clearSession()" style="background: var(--vscode-errorForeground);">Clear Context</button>
                </div>
            </div>

            <script>
                const vscode = acquireVsCodeApi();

                function escapeHtml(str) {
                    if (!str) return '';
                    return String(str)
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#039;');
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
                    const safeId = escapeHtml(backupId);
                    vscode.postMessage({ type: 'keepEdit', backupId: backupId });
                    if (btnEl && btnEl.parentElement) {
                        btnEl.parentElement.innerHTML = '<span style="color: #4ec9b0; font-weight: bold;">✓ Kept</span>';
                    }
                }
                function rejectEdit(backupId, btnEl) {
                    const safeId = escapeHtml(backupId);
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
                        const btnHtml = safeBId ? `<span style="margin-left: 8px;">` +
                            `<button onclick="keepEdit('${safeBId}', this)" style="background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; margin-right: 4px; font-weight: bold; font-size: 10px;">Keep</button>` +
                            `<button onclick="rejectEdit('${safeBId}', this)" style="background: var(--vscode-errorForeground); color: white; border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-weight: bold; font-size: 10px;">Reject</button>` +
                            `</span>` : '';
                        return `<div style="background: rgba(0,0,0,0.3); border: 1px solid var(--vscode-widget-border); padding: 6px 10px; border-radius: 6px; margin: 4px 0; font-family: var(--vscode-editor-font-family); font-size: 11px; display: flex; justify-content: space-between; align-items: center;">` +
                               `<span>📝 <code>${safeProof}-${safeDels}+${safeAdds}</code></span>` + btnHtml + `</div>`;
                    });
                }

                window.addEventListener('message', event => {
                    const message = event.data;
                    const out = document.getElementById('output');
                    
                    if (message.type === 'updateStatus') {
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
                            statusBox.id = ''; // Detach ID so next query gets a fresh box
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

export function deactivate() {
    if (serverProcess) {
        serverProcess.kill();
        serverProcess = undefined;
    }
}