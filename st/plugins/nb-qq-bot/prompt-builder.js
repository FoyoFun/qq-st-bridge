/**
 * Prompt Builder for nb-qq-bot ST Plugin
 *
 * Pure functions to construct OpenAI-format messages from character data,
 * preset templates, chat history, and user input.
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
// System prompt assembly
// ---------------------------------------------------------------------------

/**
 * Build the system message content from character data and preset.
 */
function buildSystemPrompt(character, preset, userName, qqChatBehavior) {
    const parts = [];
    const charName = (character && character.name) || '角色';

    // 1. QQ chat behavior (prepended, highest priority)
    const behavior = substituteParams(qqChatBehavior || '', charName, userName);
    if (behavior) {
        parts.push(behavior);
    }

    // 2. Character system_prompt (custom override)
    const systemPrompt = charField(character, 'system_prompt');
    if (systemPrompt) {
        parts.push(substituteParams(systemPrompt, charName, userName));
    } else {
        // Build from individual fields
        const desc = charField(character, 'description');
        if (desc) {
            parts.push('[Character: ' + charName + ']\n' + desc);
        }
        const personality = charField(character, 'personality');
        if (personality) {
            parts.push('[Personality]\n' + personality);
        }
        const scenario = charField(character, 'scenario');
        if (scenario) {
            parts.push('[Scenario]\n' + scenario);
        }
    }

    // 3. Preset main_prompt
    const mainPrompt = clean((preset && (preset.prompt || preset.main_prompt)) || '');
    if (mainPrompt) {
        parts.push(substituteParams(mainPrompt, charName, userName));
    }

    // 4. Preset jailbreak_prompt
    const jailbreak = clean((preset && preset.jailbreak_prompt) || '');
    if (jailbreak) {
        parts.push(substituteParams(jailbreak, charName, userName));
    }

    // 5. Dialogue examples (mes_example)
    const mesExample = charField(character, 'mes_example');
    if (mesExample) {
        const replaced = substituteParams(mesExample, charName, userName);
        parts.push(
            '[Example dialogue — use this tone/style:\n' +
            replaced + '\n' +
            ']'
        );
    }

    // 6. First message
    const firstMes = charField(character, 'first_mes');
    if (firstMes) {
        parts.push(
            '[Character\'s first message (for tone reference)]\n' +
            substituteParams(firstMes, charName, userName)
        );
    }

    // 7. Preset enhance_definitions_prompt
    const enhanceDefs = clean((preset && preset.enhance_definitions_prompt) || '');
    if (enhanceDefs) {
        parts.push(substituteParams(enhanceDefs, charName, userName));
    }

    // 8. Preset NSFW prompt
    const nsfwPrompt = clean((preset && preset.nsfw_prompt) || '');
    if (nsfwPrompt) {
        parts.push(substituteParams(nsfwPrompt, charName, userName));
    }

    return parts.join('\n\n') || ('You are ' + charName + '. Be helpful, engaging, and stay in character.');
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
    const systemContent = buildSystemPrompt(
        character,
        preset,
        userName,
        options.qqChatBehavior || ''
    );

    const messages = [];

    // System message
    if (systemContent) {
        messages.push({ role: 'system', content: systemContent });
    }

    // Chat history
    const history = convertHistory(chatHistory);
    for (let i = 0; i < history.length; i++) {
        messages.push(history[i]);
    }

    // Post-history instructions
    const postHistory = charField(character, 'post_history_instructions');
    if (postHistory) {
        messages.push({
            role: 'system',
            content: substituteParams(postHistory, charName, userName),
        });
    }

    // Current user message
    messages.push({ role: 'user', content: clean(userMessage) || userMessage });

    return messages;
}

module.exports = { buildMessages };
