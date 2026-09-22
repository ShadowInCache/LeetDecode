"use strict";

// ---------------------------------------------------------------------------
// Swap this between local development and your deployed Railway URL.
// No trailing slash.
// ---------------------------------------------------------------------------
const BACKEND_BASE_URL = "http://127.0.0.1:8000";

const INSTALL_ID_KEY = "leetdecode_install_id";

const el = {
  input: document.getElementById("problem-input"),
  simplify: document.getElementById("simplify-button"),
  loading: document.getElementById("loading"),
  message: document.getElementById("message"),
  messageText: document.getElementById("message-text"),
  retry: document.getElementById("retry-button"),
  results: document.getElementById("results"),
  task: document.getElementById("result-task"),
  inputText: document.getElementById("result-input"),
  outputText: document.getElementById("result-output"),
  notes: document.getElementById("result-notes"),
  exampleInput: document.getElementById("example-input"),
  exampleOutput: document.getElementById("example-output"),
  exampleExplanation: document.getElementById("example-explanation"),
  sourceBadge: document.getElementById("source-badge"),
  quota: document.getElementById("quota"),
};

// ---------------------------------------------------------------------------
// Install ID
// ---------------------------------------------------------------------------

/**
 * Return this install's anonymous id, generating and persisting one on first run.
 * This is the only identifier we ever send; there is no account and no login.
 */
async function getInstallId() {
  const stored = await chrome.storage.local.get(INSTALL_ID_KEY);
  if (stored[INSTALL_ID_KEY]) {
    return stored[INSTALL_ID_KEY];
  }
  const installId = crypto.randomUUID();
  await chrome.storage.local.set({ [INSTALL_ID_KEY]: installId });
  return installId;
}

// ---------------------------------------------------------------------------
// Quota
// ---------------------------------------------------------------------------

/**
 * Show how many free translations remain. The server is the source of truth;
 * this is display only, and a failure to fetch it is never fatal - the quota is
 * enforced server-side regardless of what the popup shows.
 */
function renderQuota(remaining) {
  if (remaining === null) {
    el.quota.hidden = true;
    return;
  }
  el.quota.textContent =
    remaining > 0
      ? `${remaining} of 5 free translations remaining`
      : "No free translations left - common problems are still free";
  el.quota.classList.toggle("quota--empty", remaining === 0);
  el.quota.hidden = false;
}

async function refreshQuota() {
  try {
    const installId = await getInstallId();
    const response = await fetch(
      `${BACKEND_BASE_URL}/usage/${encodeURIComponent(installId)}`
    );
    if (!response.ok) return;
    const body = await response.json();
    renderQuota(body.free_calls_remaining);
  } catch {
    /* Backend unreachable: leave the indicator hidden rather than guessing. */
  }
}

// ---------------------------------------------------------------------------
// View state
// ---------------------------------------------------------------------------

function setBusy(isBusy) {
  el.simplify.disabled = isBusy;
  el.simplify.textContent = isBusy ? "Simplifying..." : "Simplify Problem";
  el.loading.hidden = !isBusy;
}

function clearOutput() {
  el.results.hidden = true;
  el.message.hidden = true;
  el.retry.hidden = true;
}

/**
 * Show an error or quota message instead of results.
 * `allowRetry` controls whether the "Try again" button appears - it should not
 * for a quota error, where retrying changes nothing.
 */
function showMessage(text, { allowRetry = false } = {}) {
  el.results.hidden = true;
  el.messageText.textContent = text;
  el.retry.hidden = !allowRetry;
  el.message.hidden = false;
}

function renderResult(payload) {
  const data = payload.data;

  el.task.textContent = data.what_you_need_to_do;
  el.inputText.textContent = data.input;
  el.outputText.textContent = data.output;

  // textContent throughout, never innerHTML: this text comes from a model and
  // is rendered inside an extension page, so it is never treated as markup.
  el.notes.replaceChildren(
    ...data.important_notes.map((note) => {
      const li = document.createElement("li");
      li.textContent = note;
      return li;
    })
  );

  el.exampleInput.textContent = data.example.input;
  el.exampleOutput.textContent = data.example.output;
  el.exampleExplanation.textContent = data.example.explanation;

  el.sourceBadge.textContent =
    payload.source === "cache"
      ? "Served from cache - this one was free."
      : "Freshly generated.";

  el.message.hidden = true;
  el.results.hidden = false;
}

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

const QUOTA_MESSAGE =
  "You've used your 5 free translations. Common problems remain free - try " +
  "pasting one of LeetCode's top interview questions!";

async function requestTranslation(rawText) {
  const installId = await getInstallId();

  const response = await fetch(`${BACKEND_BASE_URL}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ install_id: installId, raw_text: rawText }),
  });

  if (response.ok) {
    return response.json();
  }

  // FastAPI nests HTTPException bodies under "detail"; tolerate both shapes so
  // the popup keeps working if the error envelope is ever flattened.
  let body = {};
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body - fall through to the status-based messages */
  }
  const detail = body.detail ?? body;

  if (response.status === 403 && detail.error === "QUOTA_EXCEEDED") {
    const quotaError = new Error(detail.message || QUOTA_MESSAGE);
    quotaError.kind = "quota";
    throw quotaError;
  }

  if (response.status === 429) {
    // Rate limited. Retrying immediately would just be refused again, so the
    // message tells the user roughly how long to wait instead.
    const retryAfter = Number(response.headers.get("Retry-After"));
    const waitHint =
      Number.isFinite(retryAfter) && retryAfter > 0
        ? ` Try again in about ${Math.ceil(retryAfter / 60)} minute${
            Math.ceil(retryAfter / 60) === 1 ? "" : "s"
          }.`
        : "";
    const limitError = new Error(
      (detail.message || "You're going a bit fast for us.") + waitHint
    );
    limitError.kind = "slow-down";
    throw limitError;
  }

  if (response.status === 503 && detail.error === "AT_CAPACITY") {
    const capacityError = new Error(detail.message);
    capacityError.kind = "capacity";
    throw capacityError;
  }

  if (response.status === 422) {
    const validationError = new Error(
      "That doesn't look like a complete problem statement. Paste the title, " +
        "description and examples, then try again."
    );
    validationError.kind = "input";
    throw validationError;
  }

  const serverError = new Error(
    detail.message ||
      "The server had trouble with that one. Please try again in a moment."
  );
  serverError.kind = "retryable";
  throw serverError;
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async function onSimplify() {
  const rawText = el.input.value.trim();

  if (rawText.length < 20) {
    showMessage(
      "Paste a full problem description first - title, what it asks for, and " +
        "the examples."
    );
    return;
  }

  clearOutput();
  setBusy(true);

  try {
    const payload = await requestTranslation(rawText);
    renderResult(payload);
    // Only a freshly generated translation can have moved the counter.
    if (payload.source === "llm") refreshQuota();
  } catch (error) {
    if (error.kind === "quota") {
      showMessage(error.message, { allowRetry: false });
      renderQuota(0);
    } else if (error.kind === "input") {
      showMessage(error.message, { allowRetry: false });
    } else if (error.kind === "slow-down" || error.kind === "capacity") {
      // Retrying now changes nothing in either case, so no retry button.
      showMessage(error.message, { allowRetry: false });
    } else if (error.kind === "retryable") {
      showMessage(error.message, { allowRetry: true });
    } else {
      // No HTTP status at all: the backend is unreachable (not running, wrong
      // BACKEND_BASE_URL, or no network).
      showMessage(
        "Couldn't reach LeetDecode. Check your connection and try again.",
        { allowRetry: true }
      );
    }
  } finally {
    setBusy(false);
  }
}

el.simplify.addEventListener("click", onSimplify);
el.retry.addEventListener("click", onSimplify);

// Ctrl/Cmd+Enter submits from the textarea.
el.input.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    onSimplify();
  }
});

// Generate the install id on first open so it exists before the first request,
// then show the remaining allowance.
getInstallId()
  .then(refreshQuota)
  .catch(() => {
    /* storage unavailable; requestTranslation will surface a usable error */
  });
