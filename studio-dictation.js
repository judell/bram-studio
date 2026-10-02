// The bridge between Main.xmlui and Bram's dictation script
// (/__shell/dictation.js, judell/bram#417), which captures the mic in the
// browser, transcribes live and writes into a field. index.html loads this
// after that script and before the XMLUI engine: the engine only sees page
// functions that exist when it starts.
//
// Markup calls these through window, passing its components:
//   studioDictationStart(box, key)   claim the mic, find the engine, start
//   studioDictationStop()            stop; resolves to the field's final text
//   studioDictationValue(key, text)  the field's onDidChange reports its text
//   studioDictationSync()            on page load and event-stream reconnect
//
// Reading the field: a component handed to page script is a snapshot, so
// box.value stays at what it was when the handler ran (a first try returned
// "" all through a dictation, and the final write emptied the field). The
// field's text is therefore pushed here from onDidChange, which fires for
// the script's own setValue writes as well as for typing.
(function () {
  var SERVER = "http://127.0.0.1:8765";
  var inst = null; // the running BramDictation.attach instance
  var key = ""; // the row being dictated into
  var latest = ""; // that row's field text
  var lines = []; // trace lines not yet sent to serve_media.py
  var flushTimer = null;

  // A string body with no Content-Type header is a "simple" request, so
  // these skip the CORS preflight a JSON POST would need each time.
  function post(path, body) {
    return fetch(SERVER + path, { method: "POST", body: JSON.stringify(body || {}) }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j && j.error) || "the media server refused (" + r.status + ")");
        return j;
      });
    });
  }

  // The script's trace lines go to serve_media.log in batches, so a
  // dictation that went wrong leaves evidence.
  function flush() {
    flushTimer = null;
    if (!lines.length) return;
    var batch = lines;
    lines = [];
    post("/dictation/trace", { key: key, lines: batch }).catch(function () {});
  }
  function trace(stage, fields) {
    lines.push({ at: Date.now(), stage: stage, fields: fields || {} });
    if (!flushTimer) flushTimer = setTimeout(flush, 1000);
  }

  window.studioDictationValue = function (noteKey, text) {
    if (key && noteKey === key) latest = String(text || "");
  };

  window.studioDictationStart = async function (box, noteKey) {
    if (key) throw new Error("already dictating");
    if (!window.BramDictation) {
      throw new Error("Dictation needs a Bram that serves /__shell/dictation.js");
    }
    await post("/dictation/start", { key: noteKey });
    key = noteKey;
    latest = String(box.value || "");
    try {
      var res = await fetch(SERVER + "/dictation/engine");
      var e = await res.json();
      if (!res.ok || !e.host) throw new Error((e && e.error) || "no speech engine");
      var d = window.BramDictation.attach({
        get: function () { return latest; },
        set: function (text) { latest = text; box.setValue(text); },
        engine: Object.assign({}, window.BramDictation.engines.whisper, {
          host: e.host,
          url: e.host + "/inference",
        }),
        trace: trace,
      });
      trace("studio-start", { engine: e.host, baseLen: latest.length });
      await d.start();
      inst = d;
      return true;
    } catch (err) {
      trace("studio-start-failed", { code: (err && err.code) || "", error: String((err && err.message) || err) });
      flush();
      key = "";
      post("/dictation/stop", {}).catch(function () {});
      throw err;
    }
  };

  window.studioDictationStop = async function () {
    if (!inst) return null;
    var d = inst;
    inst = null;
    try {
      await d.stop(); // writes the final text into the field, through set()
    } finally {
      trace("studio-stop", { finalLen: latest.length });
      flush();
      key = "";
      post("/dictation/stop", {}).catch(function () {});
    }
    return latest;
  };

  // The page and the server agree on who is dictating: a page that is
  // dictating re-claims (the server may have restarted); one that isn't
  // releases whatever claim an earlier load of it left behind. Returns the
  // row being dictated into, or "".
  window.studioDictationSync = function () {
    post(key ? "/dictation/start" : "/dictation/stop", key ? { key: key } : {}).catch(function () {});
    return key;
  };

  // A page going away mid-dictation loses its capture; give up the claim.
  window.addEventListener("pagehide", function () {
    if (key) navigator.sendBeacon(SERVER + "/dictation/stop", "{}");
  });
})();
