/* No side effects on import. Shared browser/CommonJS submission contract. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DurableWork = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const ACTIVE = new Set(['queued', 'running', 'retry_pending']);
  function shouldPoll(state) { return ACTIVE.has(state); }
  function newSubmission(conversationId, generation, text, requestKey) {
    return Object.freeze({ conversation_id: conversationId, generation, text, request_key: requestKey });
  }
  function retrySubmission(pending) { return { ...pending }; }
  return { shouldPoll, newSubmission, retrySubmission };
});
