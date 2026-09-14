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
    description: 'Unified prompt-building + generation endpoint for the qq-st-bridge NoneBot2 plugin.',
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
 * Read the model ST's UI would use right now, from the live settings
 * payload (/api/settings/get). That response carries the full settings.json
 * content as a string under `settings`; the UI's connection state lives in
 * its `oai_settings` object. Same state the ST interface loads, so whatever
 * is configured there applies here automatically — no per-environment
 * config needed. Model keys are per source: `deepseek_model`, `openai_model`, …
 */
function readLiveModel(settings, source) {
    if (!settings || typeof settings !== 'object' || !source) return null;
    let live = null;
    try {
        const parsed = typeof settings.settings === 'string'
            ? JSON.parse(settings.settings)
            : settings;
        live = parsed && typeof parsed === 'object' ? parsed.oai_settings : null;
    } catch (e) {
        return null; // unparsable settings — let the fallbacks handle it
    }
    if (!live || typeof live !== 'object') return null;
    const found = live[source + '_model'];
    if (!found) return null;
    if (live.chat_completion_source !== source) {
        console.log('[nb-qq-bot] Note: ST UI is on source "' + live.chat_completion_source
            + '"; using its saved "' + source + '" model anyway: ' + found);
    } else {
        console.log('[nb-qq-bot] Resolved model from ST live settings: ' + found);
    }
    return found;
}

/**
 * Fallback: read the model for a given source from ST's OpenAI Settings
 * files on disk. Each connection profile is a JSON file under
 * data/<user>/OpenAI Settings/ storing the model as `{source}_model`.
 *
 * Preference: a profile whose chat_completion_source matches the requested
 * source. If none declares it, fall back to any profile carrying a non-empty
 * `{source}_model` — profiles keep settings for many sources but only declare
 * the one they were last saved with.
 */
function readModelFromConnection(source) {
    if (!source) return null;
    try {
        const settingsDir = path.join(ST_ROOT, 'data', 'default-user', 'OpenAI Settings');
        if (!fs.existsSync(settingsDir)) return null;
        const files = fs.readdirSync(settingsDir).filter(function (f) { return f.endsWith('.json'); });
        const modelField = source + '_model';
        let loose = null;
        for (let i = 0; i < files.length; i++) {
            try {
                const filePath = path.join(settingsDir, files[i]);
                const raw = fs.readFileSync(filePath, 'utf8');
                const data = JSON.parse(raw);
                const found = data[modelField];
                if (!found) continue;
                if (data.chat_completion_source === source) {
                    console.log('[nb-qq-bot] Resolved model from connection "' + path.basename(files[i], '.json') + '": ' + found);
                    return found;
                }
                if (!loose) loose = found;
            } catch (e) { /* skip unreadable files */ }
        }
        if (loose) {
            console.log('[nb-qq-bot] No profile declares source "' + source + '"; using its ' + modelField + ' anyway: ' + loose);
        }
        return loose;
    } catch (e) {
        console.warn('[nb-qq-bot] Failed to read connection settings:', e.message);
    }
    return null;
}

// Live model lists per source, cached briefly — validation must not add an
// upstream roundtrip to every generation.
const MODEL_LIST_TTL_MS = 10 * 60 * 1000;
const _modelListCache = new Map(); // source -> { at: number, ids: string[] }

/**
 * Ask ST which models the given source currently offers
 * (POST /api/backends/chat-completions/status → { data: [{ id }] }).
 * Returns null when the list can't be fetched — callers then skip validation.
 */
async function fetchAvailableModels(source, csrfToken, cookieJar) {
    if (!source) return null;
    const cached = _modelListCache.get(source);
    if (cached && (Date.now() - cached.at) < MODEL_LIST_TTL_MS) {
        return cached.ids;
    }
    try {
        const data = await internalPost('/api/backends/chat-completions/status', {
            chat_completion_source: source,
            reverse_proxy: '',
        }, csrfToken, cookieJar);
        const ids = (data && Array.isArray(data.data))
            ? data.data
                .filter(function (m) { return m && typeof m.id === 'string'; })
                .map(function (m) { return m.id; })
                .sort()
            : null;
        if (ids && ids.length > 0) {
            _modelListCache.set(source, { at: Date.now(), ids: ids });
        }
        return ids;
    } catch (e) {
        console.warn('[nb-qq-bot] Could not list models for "' + source + '":', e.message);
        return null;
    }
}

const MODEL_TIER_WORDS = ['flash', 'pro', 'lite', 'mini', 'chat', 'reasoner'];

/**
 * Final model pick: keep `resolvedModel` when the source still offers it;
 * otherwise fall back to the closest offered model (same tier word first),
 * so an upstream rename degrades to a logged warning instead of a 400.
 */
async function resolveModel(resolvedModel, source, csrfToken, cookieJar) {
    const available = await fetchAvailableModels(source, csrfToken, cookieJar);
    if (!available || available.length === 0) {
        return resolvedModel; // can't validate — send as-is
    }
    if (resolvedModel && available.indexOf(resolvedModel) >= 0) {
        return resolvedModel;
    }
    let fallback = available[0];
    if (resolvedModel) {
        const lower = resolvedModel.toLowerCase();
        const tier = MODEL_TIER_WORDS.find(function (w) { return lower.indexOf(w) >= 0; });
        if (tier) {
            const match = available.find(function (id) { return id.toLowerCase().indexOf(tier) >= 0; });
            if (match) fallback = match;
        }
        console.warn('[nb-qq-bot] Model "' + resolvedModel + '" is no longer offered by "' + source + '".');
    } else {
        console.warn('[nb-qq-bot] No model configured for source "' + source + '".');
    }
    console.warn('[nb-qq-bot] Falling back to "' + fallback + '". Available: ' + available.join(', '));
    return fallback;
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
            options: {
                qqChatBehavior: qq_chat_behavior,
                postHistory: body.post_history_instructions || '',
            },
        });

        // --- 4. Assemble generate payload ---
        const generatePayload = {
            messages: messages,
            chat_completion_source: chat_completion_source || preset.chat_completion_source || 'deepseek',
            stream: stream,
            max_tokens: max_response_length || preset.openai_max_tokens || preset.max_tokens || 500,
        };

        // Model — explicit override, else the model ST's UI is currently
        // using, else profile files; validated against what the source
        // currently offers so upstream renames degrade to a warning
        const effectiveSource = generatePayload.chat_completion_source;
        const resolvedModel = model
            || readLiveModel(settings, effectiveSource)
            || readModelFromConnection(effectiveSource);
        const finalModel = await resolveModel(resolvedModel, effectiveSource, csrfToken, cookieJar);
        if (finalModel) {
            generatePayload.model = finalModel;
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

// Internals exported for testing; ST itself only uses info/init.
module.exports = {
    info, init,
    CookieJar, internalPost, fetchCsrfToken,
    readLiveModel, readModelFromConnection, fetchAvailableModels, resolveModel,
};
