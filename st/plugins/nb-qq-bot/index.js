/**
 * nb-qq-bot SillyTavern Plugin
 *
 * Provides a unified API endpoint for the QQ bot to call, which:
 * 1. Loads character data from ST
 * 2. Loads preset/settings from ST
 * 3. Builds a high-quality prompt (matching ST's frontend approach)
 * 4. Calls the AI generate endpoint
 * 5. Returns the response
 *
 * Mounted at: POST /api/plugins/nb-qq-bot/generate
 *
 * CommonJS module — compatible with ST's plugin loader.
 * Independent of ST source code — no imports from src/.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const { buildMessages } = require('./prompt-builder.js');

// ---------------------------------------------------------------------------
// Plugin metadata (required by ST plugin-loader)
// ---------------------------------------------------------------------------

const info = {
    id: 'nb-qq-bot',
    name: 'NB QQ Bot Bridge',
    description: 'Unified prompt-building + generation endpoint for the nb_qq_bot NoneBot2 plugin.',
};

// ---------------------------------------------------------------------------
// Config reader (reads ST's config.yaml port)
// ---------------------------------------------------------------------------

const ST_ROOT = path.resolve(__dirname, '..', '..');
const CONFIG_PATH = path.join(ST_ROOT, 'config.yaml');

let _stPort = null;

function getPort() {
    if (_stPort !== null) return _stPort;
    try {
        const content = fs.readFileSync(CONFIG_PATH, 'utf8');
        const match = content.match(/^port\s*:\s*(\d+)/m);
        _stPort = match ? parseInt(match[1], 10) : 8000;
    } catch (e) {
        _stPort = 8000;
    }
    return _stPort;
}

// ---------------------------------------------------------------------------
// Internal HTTP helpers (calls ST's own API on localhost)
// ---------------------------------------------------------------------------

/**
 * Simple cookie jar: stores cookies from set-cookie headers.
 */
class CookieJar {
    constructor() {
        this.cookies = '';
    }

    update(headers) {
        const setCookie = headers.get('set-cookie');
        if (setCookie) {
            const parts = setCookie.split(',').map(function (s) { return s.trim(); });
            for (let i = 0; i < parts.length; i++) {
                const part = parts[i];
                const semiIdx = part.indexOf(';');
                const cookie = semiIdx >= 0 ? part.substring(0, semiIdx) : part;
                const eqIdx = cookie.indexOf('=');
                if (eqIdx >= 0) {
                    const name = cookie.substring(0, eqIdx);
                    const existingIdx = this.cookies.indexOf(name + '=');
                    if (existingIdx >= 0) {
                        const endIdx = this.cookies.indexOf(';', existingIdx);
                        if (endIdx >= 0) {
                            this.cookies = this.cookies.substring(0, existingIdx) + cookie + this.cookies.substring(endIdx);
                        } else {
                            this.cookies = this.cookies.substring(0, existingIdx) + cookie;
                        }
                    } else {
                        this.cookies += (this.cookies ? '; ' : '') + cookie;
                    }
                }
            }
        }
    }

    getHeader() {
        return this.cookies;
    }
}

/**
 * Make an internal HTTP POST request to ST's own API on localhost.
 */
async function internalPost(apiPath, body, csrfToken, cookieJar) {
    const port = getPort();
    const url = 'http://127.0.0.1:' + port + apiPath;

    const headers = {
        'Content-Type': 'application/json',
    };
    if (csrfToken) {
        headers['X-CSRF-Token'] = csrfToken;
    }
    const cookieHeader = cookieJar.getHeader();
    if (cookieHeader) {
        headers['Cookie'] = cookieHeader;
    }

    const resp = await fetch(url, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(body),
    });

    cookieJar.update(resp.headers);

    if (!resp.ok) {
        const text = await resp.text().catch(function () { return ''; });
        let detail = text || ('HTTP ' + resp.status);
        try {
            const err = JSON.parse(text);
            if (typeof err === 'object' && err !== null) {
                if (typeof err.error === 'string') {
                    detail = err.error;
                } else if (typeof err.error === 'object' && err.error && typeof err.error.message === 'string') {
                    detail = err.error.message;
                } else if (typeof err.message === 'string') {
                    detail = err.message;
                } else {
                    detail = JSON.stringify(err);
                }
            }
        } catch (e) { /* use raw text */ }
        console.error('[nb-qq-bot] ST API error (' + apiPath + '):', detail);
        throw new Error('ST API error (' + apiPath + '): ' + detail);
    }

    return resp.json();
}

/**
 * Fetch a CSRF token and initialize the session cookie.
 */
async function fetchCsrfToken(cookieJar) {
    const port = getPort();
    const url = 'http://127.0.0.1:' + port + '/csrf-token';

    const resp = await fetch(url);
    cookieJar.update(resp.headers);

    if (!resp.ok) {
        throw new Error('Failed to fetch CSRF token: HTTP ' + resp.status);
    }

    const data = await resp.json();
    return data.token;
}

// ---------------------------------------------------------------------------
// Model resolution from ST connection settings
// ---------------------------------------------------------------------------

/**
 * Read the model for a given source from ST's OpenAI Settings files.
 * Each connection profile is stored as a JSON file under data/<user>/OpenAI Settings/.
 * The model is stored as `{source}_model` (e.g. `deepseek_model`).
 */
function readModelFromConnection(source) {
    if (!source) return null;
    try {
        const settingsDir = path.join(ST_ROOT, 'data', 'default-user', 'OpenAI Settings');
        if (!fs.existsSync(settingsDir)) return null;
        const files = fs.readdirSync(settingsDir).filter(function (f) { return f.endsWith('.json'); });
        for (let i = 0; i < files.length; i++) {
            try {
                const filePath = path.join(settingsDir, files[i]);
                const raw = fs.readFileSync(filePath, 'utf8');
                const data = JSON.parse(raw);
                if (data.chat_completion_source === source) {
                    const modelField = source + '_model';
                    const found = data[modelField];
                    if (found) {
                        console.log('[nb-qq-bot] Resolved model from connection "' + path.basename(files[i], '.json') + '": ' + found);
                        return found;
                    }
                }
            } catch (e) { /* skip unreadable files */ }
        }
    } catch (e) {
        console.warn('[nb-qq-bot] Failed to read connection settings:', e.message);
    }
    return null;
}

// ---------------------------------------------------------------------------
// Plugin route handler
// ---------------------------------------------------------------------------

/**
 * POST /generate
 */
async function handleGenerate(req, res) {
    try {
        const body = req.body || {};

        // --- Validate required fields ---
        const required = ['avatar_url', 'preset_name', 'user_message'];
        for (let i = 0; i < required.length; i++) {
            if (!body[required[i]]) {
                return res.json({ success: false, error: 'Missing required field: ' + required[i] });
            }
        }

        const avatar_url = body.avatar_url;
        const preset_name = body.preset_name;
        const chat_history = body.chat_history || [];
        const user_message = body.user_message;
        const user_name = body.user_name || 'QQ用户';
        const qq_chat_behavior = body.qq_chat_behavior || '';
        const max_response_length = body.max_response_length || 800;
        const chat_completion_source = body.chat_completion_source || 'deepseek';
        const model = body.model || '';
        const stream = body.stream || false;

        // --- Initialize session ---
        const cookieJar = new CookieJar();
        const csrfToken = await fetchCsrfToken(cookieJar);

        // --- 1. Fetch character data ---
        let character;
        try {
            character = await internalPost('/api/characters/get', { avatar_url: avatar_url }, csrfToken, cookieJar);
        } catch (e) {
            return res.json({ success: false, error: 'Character not found: ' + avatar_url });
        }

        // --- 2. Fetch settings & find preset ---
        let settings;
        try {
            settings = await internalPost('/api/settings/get', {}, csrfToken, cookieJar);
        } catch (e) {
            return res.json({ success: false, error: 'Failed to load ST settings' });
        }

        const presetNames = settings.openai_setting_names || [];
        const presetContents = settings.openai_settings || [];
        const presetIdx = presetNames.indexOf(preset_name);

        if (presetIdx < 0) {
            return res.json({ success: false, error: 'Preset not found: ' + preset_name });
        }

        let preset;
        try {
            const raw = presetContents[presetIdx];
            preset = typeof raw === 'string' ? JSON.parse(raw) : raw;
        } catch (e) {
            return res.json({ success: false, error: 'Failed to parse preset: ' + preset_name });
        }

        // --- 3. Build messages ---
        const messages = buildMessages({
            character: character,
            preset: preset,
            chatHistory: chat_history,
            userMessage: user_message,
            userName: user_name,
            options: { qqChatBehavior: qq_chat_behavior },
        });

        // --- 4. Assemble generate payload ---
        const generatePayload = {
            messages: messages,
            chat_completion_source: chat_completion_source || preset.chat_completion_source || 'deepseek',
            stream: stream,
            max_tokens: max_response_length || preset.openai_max_tokens || preset.max_tokens || 500,
        };

        // Model — use explicitly passed model, or read from ST connection settings
        const effectiveSource = generatePayload.chat_completion_source;
        const resolvedModel = model || readModelFromConnection(effectiveSource);
        if (resolvedModel) {
            generatePayload.model = resolvedModel;
        }

        // Generation parameters from preset
        const presetKeys = [
            'temperature', 'frequency_penalty', 'presence_penalty',
            'top_p', 'top_k', 'top_a', 'min_p', 'repetition_penalty', 'thinking',
        ];
        for (let i = 0; i < presetKeys.length; i++) {
            const key = presetKeys[i];
            if (preset[key] !== undefined) {
                generatePayload[key] = preset[key];
            }
        }

        // --- 5. Call generate ---
        let generateResult;
        try {
            generateResult = await internalPost(
                '/api/backends/chat-completions/generate',
                generatePayload,
                csrfToken,
                cookieJar
            );
        } catch (e) {
            return res.json({ success: false, error: 'AI generation failed: ' + e.message });
        }

        // --- 6. Extract response ---
        let responseText = '';
        if (generateResult.choices && generateResult.choices.length > 0) {
            const msg = generateResult.choices[0].message;
            responseText = (msg && msg.content) || '';
        }

        if (!responseText || !responseText.trim()) {
            return res.json({ success: false, error: 'AI returned empty response' });
        }

        return res.json({ success: true, response_text: responseText });

    } catch (e) {
        console.error('[nb-qq-bot] Unexpected error:', e);
        return res.json({ success: false, error: 'Internal error: ' + e.message });
    }
}

// ---------------------------------------------------------------------------
// Plugin initialization
// ---------------------------------------------------------------------------

/**
 * Called by ST's plugin-loader to initialize this plugin.
 * @param {import('express').Router} router - Express Router for this plugin
 */
async function init(router) {
    router.post('/generate', handleGenerate);
    console.log('[nb-qq-bot] Plugin initialized — /api/plugins/nb-qq-bot/generate');
}

module.exports = { info, init };
