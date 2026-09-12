/**
 * Prompt Builder for nb-qq-bot ST Plugin
 *
 * Pure functions to construct OpenAI-format messages from character data,
 * preset templates, chat history, and user input.
 *
 * Preset formats supported:
 * 1. ST-native: prompts[] array + prompt_order (identifier-based, exactly
 *    what SillyTavern's own prompt manager consumes). This lets the preset
 *    JSON edited in ST's UI drive the persona text.
 * 2. Flat fields (prompt / main_prompt / jailbreak_prompt) — legacy
 *    fallback when no prompt_order is present.
 *
 * CommonJS module — compatible with ST's plugin loader.
 */

'use strict';

// ---------------------------------------------------------------------------
// Macro substitution
// ---------------------------------------------------------------------------

/**
 * Replace {{char}} and {{user}} macros in a template string.
 */
function substituteParams(text, charName, userName) {
    if (!text) return '';
    return text
        .replace(/\{\{char\}\}/gi, charName)
        .replace(/\{\{user\}\}/gi, userName);
}

// ---------------------------------------------------------------------------
// Character data extraction
// ---------------------------------------------------------------------------

/**
 * Normalize line endings and trim whitespace.
 */
function clean(s) {
    if (!s) return '';
    return s.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
}

/**
 * Get the effective value of a character field, checking both
 * top-level and data.* (Spec V2) locations.
 */
function charField(character, field) {
    const data = character && character.data ? character.data : {};
    return clean(character && character[field] || '') || clean(data[field] || '');
}

// ---------------------------------------------------------------------------
// Preset prompt resolution (ST-native prompts[] + prompt_order)
// ---------------------------------------------------------------------------

/**
 * Map preset identifier -> prompt entry (only entries that carry content).
 */
function presetPromptMap(preset) {
    const map = {};
    const list = preset && Array.isArray(preset.prompts) ? preset.prompts : [];
    for (const entry of list) {
        if (entry && entry.identifier && !entry.marker && typeof entry.content === 'string') {
            map[entry.identifier] = entry;
        }
    }
    return map;
}

/**
 * Resolve the enabled prompt order for character prompts (100001 preferred,
 * falling back to 100000). Returns [] when the preset has no usable order.
 */
function presetOrder(preset) {
    const orders = preset && Array.isArray(preset.prompt_order) ? preset.prompt_order : [];
    for (const characterId of [100001, 100000]) {
        const found = orders.find((o) => o && o.character_id === characterId && Array.isArray(o.order));
        if (found) {
            return found.order.filter((item) => item && item.enabled !== false);
        }
    }
    return [];
}

/**
 * Collect a single named prompt's content from the native map.
 */
function nativePrompt(promptMap, identifier) {
    const entry = promptMap[identifier];
    return entry ? clean(entry.content) : '';
}

/**
 * Build the system message content from character data and preset.
 * Post-history content ("jailbreak" identifier / jailbreak_prompt /
 * character post_history_instructions) is returned separately — it must
 * be injected AFTER the chat history, not into the system message.
 */
function buildSystemPrompt(character, preset, userName, qqChatBehavior) {
    const parts = [];
    const charName = (character && character.name) || '角色';

    // 0. Bridge operational behavior (input format + output contract)
    const behavior = substituteParams(qqChatBehavior || '', charName, userName);
    if (behavior) {
        parts.push(behavior);
    }

    const promptMap = presetPromptMap(preset);
    const order = presetOrder(preset);

    if (order.length > 0) {
        // --- ST-native assembly, honoring prompt_order ---
        for (const item of order) {
            const id = item.identifier;
            if (id === 'main') {
                const main = nativePrompt(promptMap, 'main');
                if (main) parts.push(substituteParams(main, charName, userName));
            } else if (id === 'charDescription') {
                const desc = charField(character, 'description');
                if (desc) parts.push('[Character: ' + charName + ']\n' + substituteParams(desc, charName, userName));
            } else if (id === 'charPersonality') {
                const personality = charField(character, 'personality');
                if (personality) parts.push('[Personality]\n' + substituteParams(personality, charName, userName));
            } else if (id === 'scenario') {
                const scenario = charField(character, 'scenario');
                if (scenario) parts.push('[Scenario]\n' + substituteParams(scenario, charName, userName));
            } else if (id === 'dialogueExamples') {
                const mesExample = charField(character, 'mes_example');
                if (mesExample) {
                    parts.push(
                        '[Example dialogue — mimic this tone and style:\n' +
                        substituteParams(mesExample, charName, userName) + '\n' +
                        ']'
                    );
                }
            } else if (id === 'enhanceDefinitions' && item.enabled !== false) {
                const enhance = nativePrompt(promptMap, 'enhanceDefinitions');
                if (enhance && item.enabled !== false && enhance) {
                    parts.push(substituteParams(enhance, charName, userName));
                }
            } else if (id === 'nsfw') {
                const nsfw = nativePrompt(promptMap, 'nsfw');
                if (nsfw) parts.push(substituteParams(nsfw, charName, userName));
            }
            // chatHistory / worldInfo* / personaDescription markers are
            // handled elsewhere or not supported by the bridge.
        }
    } else {
        // --- Legacy flat-field fallback ---
        const systemPrompt = charField(character, 'system_prompt');
        if (systemPrompt) {
            parts.push(substituteParams(systemPrompt, charName, userName));
        } else {
            const desc = charField(character, 'description');
            if (desc) parts.push('[Character: ' + charName + ']\n' + desc);
            const personality = charField(character, 'personality');
            if (personality) parts.push('[Personality]\n' + personality);
            const scenario = charField(character, 'scenario');
            if (scenario) parts.push('[Scenario]\n' + scenario);
        }

        const mainPrompt = clean((preset && (preset.prompt || preset.main_prompt)) || '');
        if (mainPrompt) parts.push(substituteParams(mainPrompt, charName, userName));

        const mesExample = charField(character, 'mes_example');
        if (mesExample) {
            parts.push(
                '[Example dialogue — use this tone/style:\n' +
                substituteParams(mesExample, charName, userName) + '\n' +
                ']'
            );
        }

        const firstMes = charField(character, 'first_mes');
        if (firstMes) {
            parts.push(
                '[Character\'s first message (for tone reference)]\n' +
                substituteParams(firstMes, charName, userName)
            );
        }

        const enhanceDefs = clean((preset && preset.enhance_definitions_prompt) || '');
        if (enhanceDefs) parts.push(substituteParams(enhanceDefs, charName, userName));

        const nsfwPrompt = clean((preset && preset.nsfw_prompt) || '');
        if (nsfwPrompt) parts.push(substituteParams(nsfwPrompt, charName, userName));
    }

    // First message tone reference (native path misses it above)
    if (order.length > 0) {
        const firstMes = charField(character, 'first_mes');
        if (firstMes) {
            parts.push(
                '[Character\'s first message (for tone reference)]\n' +
                substituteParams(firstMes, charName, userName)
            );
        }
    }

    return {
        system: parts.join('\n\n') || ('You are ' + charName + '. Be helpful, engaging, and stay in character.'),
        postHistory: buildPostHistory(character, preset, promptMap, charName, userName),
    };
}

/**
 * Post-history instruction content, in priority order:
 * explicit bridge contract > character field > preset native "jailbreak"
 * > preset flat jailbreak_prompt. All configured layers are concatenated.
 */
function buildPostHistory(character, preset, promptMap, charName, userName) {
    const parts = [];
    const charPhi = charField(character, 'post_history_instructions');
    if (charPhi) parts.push(substituteParams(charPhi, charName, userName));
    const nativeJailbreak = nativePrompt(promptMap, 'jailbreak');
    if (nativeJailbreak) {
        parts.push(substituteParams(nativeJailbreak, charName, userName));
    } else {
        const flat = clean((preset && preset.jailbreak_prompt) || '');
        if (flat) parts.push(substituteParams(flat, charName, userName));
    }
    return parts.join('\n\n');
}

// ---------------------------------------------------------------------------
// Chat history conversion
// ---------------------------------------------------------------------------

/**
 * Convert ST JSONL chat history to OpenAI message format.
 */
function convertHistory(chatHistory) {
    const messages = [];
    for (let i = 0; i < chatHistory.length; i++) {
        const msg = chatHistory[i];
        // Skip header lines
        if (msg.chat_metadata) continue;
        const content = clean(msg.mes || msg.content || '');
        if (!content) continue;
        const role = msg.is_user ? 'user' : 'assistant';
        messages.push({ role: role, content: content });
    }
    return messages;
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

/**
 * Build the complete OpenAI-format messages array.
 */
function buildMessages(params) {
    const character = params.character || {};
    const preset = params.preset || {};
    const chatHistory = params.chatHistory || [];
    const userMessage = params.userMessage || '';
    const userName = params.userName || 'QQ用户';
    const options = params.options || {};

    const charName = character.name || '';
    const built = buildSystemPrompt(character, preset, userName, options.qqChatBehavior || '');

    const messages = [];

    // System message
    if (built.system) {
        messages.push({ role: 'system', content: built.system });
    }

    // Chat history
    const history = convertHistory(chatHistory);
    for (let i = 0; i < history.length; i++) {
        messages.push(history[i]);
    }

    // Post-history instructions (bridge contract > character > preset),
    // then always the bridge-side output protocol recap as the final
    // system message (highest recency).
    const postHistoryParts = [];
    if (built.postHistory) postHistoryParts.push(built.postHistory);
    const bridgePostHistory = clean(options.postHistory || '');
    if (bridgePostHistory) postHistoryParts.push(bridgePostHistory);
    if (postHistoryParts.length > 0) {
        messages.push({ role: 'system', content: postHistoryParts.join('\n\n') });
    }

    // Current user message
    messages.push({ role: 'user', content: clean(userMessage) || userMessage });

    return messages;
}

module.exports = { buildMessages };
