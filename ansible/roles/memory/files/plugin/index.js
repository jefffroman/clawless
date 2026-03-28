"use strict";

const http = require("http");

const DEFAULTS = {
  serverUrl: "http://127.0.0.1:3271",
  topN: 5,
  compact: true,
  timeoutMs: 2000,
};

/**
 * POST JSON to the memory server. Returns parsed response or null on error.
 */
function postJSON(url, body, timeoutMs) {
  return new Promise((resolve) => {
    const data = JSON.stringify(body);
    const parsed = new URL(url);
    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port,
        path: parsed.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
        },
        timeout: timeoutMs,
      },
      (res) => {
        let chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString()));
          } catch {
            resolve(null);
          }
        });
      }
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
    req.write(data);
    req.end();
  });
}

/**
 * Plugin entry point — called by OpenClaw's plugin loader via Jiti.
 */
module.exports = function register(api) {
  api.registerContextEngine("clawless-memory", (pluginConfig) => {
    const cfg = { ...DEFAULTS, ...pluginConfig };

    return {
      info: {
        id: "clawless-memory",
        name: "Clawless 3-Layer Memory",
        version: "0.1.0",
      },

      async ingest() {
        return { ingested: false };
      },

      async assemble(params) {
        const prompt = params.prompt;
        if (!prompt) {
          return { messages: params.messages, estimatedTokens: 0 };
        }

        const result = await postJSON(
          `${cfg.serverUrl}/retrieve`,
          { query: prompt, top_n: cfg.topN, compact: cfg.compact },
          cfg.timeoutMs
        );

        if (!result || !result.markdown) {
          return { messages: params.messages, estimatedTokens: 0 };
        }

        // Respect token budget: rough estimate at 4 chars/token.
        if (
          params.tokenBudget &&
          result.tokens_est > params.tokenBudget * 0.3
        ) {
          return { messages: params.messages, estimatedTokens: 0 };
        }

        // Prepend a system message with the retrieved memory context.
        const memoryMessage = {
          role: "system",
          content: result.markdown,
        };

        return {
          messages: [memoryMessage, ...params.messages],
          estimatedTokens: result.tokens_est,
        };
      },

      async compact() {
        return { ok: true, compacted: false };
      },
    };
  });
};
