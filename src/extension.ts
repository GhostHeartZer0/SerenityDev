import * as vscode from 'vscode';
import axios from 'axios';
import * as http from 'http';

const API_BASE = 'http://localhost:8002/api';
const ASK_URL = 'http://localhost:8002/ask';

export function activate(context: vscode.ExtensionContext) {
    let statusBarItem: vscode.StatusBarItem;

    // Initialize Status Bar
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.command = 'serenity.showMenu';
    context.subscriptions.push(statusBarItem);

    // Register Native VS Code Chat Participant
    const chatParticipant = vscode.chat.createChatParticipant('serenitydev.assistant', async (request: vscode.ChatRequest, context: vscode.ChatContext, response: vscode.ChatResponseStream, token: vscode.CancellationToken) => {
        response.progress('Initializing SerenityDev routing pipeline...');

        return new Promise<void>((resolve, reject) => {
            const postData = JSON.stringify({
                prompt: request.prompt,
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
                                    response.markdown(new vscode.MarkdownString(data.content));
                                } else if (data.type === 'error' && data.detail) {
                                    response.markdown(new vscode.MarkdownString(`❌ **Error:** ${data.detail}`));
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
                                    response.markdown(new vscode.MarkdownString(routingInfo));
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
                                response.markdown(new vscode.MarkdownString(data.content));
                            }
                        } catch (e) { }
                    }
                    resolve();
                });
            });

            req.on('error', (err: any) => {
                response.markdown(new vscode.MarkdownString(`❌ **Error calling SerenityDev server:** ${err.message}`));
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
            const models: vscode.LanguageModelChatInformation[] = [
                {
                    id: 'serenity-supervisor',
                    name: 'Serenity: gemma-4-26B-A4B (Supervisor)',
                    family: 'serenity-supervisor',
                    version: '1.0.0',
                    maxInputTokens: 120000,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                },
                {
                    id: 'serenity-w1',
                    name: 'Serenity: gemma-4-26B-A4B (Worker 1)',
                    family: 'serenity-w1',
                    version: '1.0.0',
                    maxInputTokens: 120000,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                },
                {
                    id: 'serenity-w2',
                    name: 'Serenity: codegemma-7b-it (Worker 2)',
                    family: 'serenity-w2',
                    version: '1.0.0',
                    maxInputTokens: 120000,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                },
                {
                    id: 'serenity-w3',
                    name: 'Serenity: Qwen3.6 35B-A3B (Worker 3)',
                    family: 'serenity-w3',
                    version: '1.0.0',
                    maxInputTokens: 120000,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                },
                {
                    id: 'serenity-w4',
                    name: 'Serenity: Qwen3.6 27B (Worker 4)',
                    family: 'serenity-w4',
                    version: '1.0.0',
                    maxInputTokens: 120000,
                    maxOutputTokens: 16384,
                    capabilities: { toolCalling: true, imageInput: false }
                }
            ];
            return models;
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

    constructor(private readonly _extensionUri: vscode.Uri) {
        this._sessionId = 'session_' + Math.random().toString(36).substr(2, 9);
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
                this._sessionId = 'session_' + Math.random().toString(36).substr(2, 9);
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
                
                function submitQuery() {
                    const input = document.getElementById('promptInput');
                    const text = input.value.trim();
                    if(!text) return;

                    // Append user text
                    const out = document.getElementById('output');
                    out.innerHTML += '<div class="msg user"><strong>You:</strong> ' + text + '</div>';
                    
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

                window.addEventListener('message', event => {
                    const message = event.data;
                    const out = document.getElementById('output');
                    
                    if (message.type === 'updateStatus') {
                        const statusBox = document.getElementById('statusBox');
                        if (statusBox) {
                            let logHtml = '';
                            message.logs.forEach(l => { logHtml += '<div>⚙️ ' + l + '</div>'; });
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
                            routingHtml = '<div class="routing-box">🗺️ Plan Step Count: ' + (message.routing.steps ? message.routing.steps.length : 0) + '<br/>Worker: ' + message.routing.worker + '</div>';
                        }
                        out.innerHTML += '<div class="msg agent"><strong>SerenityDev:</strong> ' + message.value + routingHtml + '</div>';
                    } else if (message.type === 'addError') {
                        out.innerHTML += '<div class="msg" style="color: var(--vscode-errorForeground)">❌ Error: ' + message.value + '</div>';
                    }
                    out.scrollTop = out.scrollHeight;
                });
            </script>
        </body>
        </html>`;
    }
}

export function deactivate() { }