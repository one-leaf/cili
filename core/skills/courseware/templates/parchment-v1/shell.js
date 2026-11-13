(function () {
  "use strict";

  var META = window.CW_META || {};
  var scenes = Array.prototype.slice.call(document.querySelectorAll(".cw-scene"));
  var total = scenes.length;
  var idx = 0;
  var attemptId = "att-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);

  var viewport = document.getElementById("cwViewport");
  var stage = document.getElementById("cwStage");
  var pageNo = document.getElementById("cwPageNo");
  var progressBar = document.getElementById("cwProgressBar");
  var notes = document.getElementById("cwNotes");
  var notesBody = document.getElementById("cwNotesBody");
  var notesBtn = document.getElementById("cwNotesBtn");

  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  function scaleStage() {
    if (!viewport || !stage) return;
    var rect = viewport.getBoundingClientRect();
    var aw = Math.max(320, rect.width - 16);
    var ah = Math.max(240, rect.height - 8);
    var s = Math.min(aw / 1280, ah / 720, 1.35);
    stage.style.setProperty("--cw-scale", String(Math.max(0.24, s)));
  }

  function currentNotes() {
    var scene = scenes[idx];
    if (!scene) return "";
    var src = scene.querySelector(".cw-notes-src");
    return src ? (src.textContent || "").trim() : "";
  }

  function updateNotes() {
    if (!notes || notes.hidden) return;
    var text = currentNotes();
    notesBody.textContent = text || "本页没有讲稿。";
  }

  function go(i) {
    if (!total) return;
    idx = clamp(i, 0, total - 1);
    scenes.forEach(function (s, n) { s.classList.toggle("is-active", n === idx); });
    if (pageNo) pageNo.textContent = (idx + 1) + " / " + total;
    if (progressBar) progressBar.style.width = (((idx + 1) / total) * 100).toFixed(2) + "%";
    updateNotes();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  }

  function toggleNotes(force) {
    if (!notes) return;
    var open = typeof force === "boolean" ? force : notes.hidden;
    notes.hidden = !open;
    if (notesBtn) notesBtn.classList.toggle("is-on", open);
    if (open) updateNotes();
  }

  function toggleFull() {
    try {
      if (!document.fullscreenElement) {
        var p = document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
        if (p && p.catch) p.catch(function () {});
      } else if (document.exitFullscreen) {
        document.exitFullscreen();
      }
    } catch (e) { /* sandboxed preview may block fullscreen */ }
  }

  function normalizeText(v) {
    return String(v == null ? "" : v).replace(/\s+/g, " ").trim();
  }

  function letters(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(String.fromCharCode(65 + i));
    return out;
  }

  function updateQuizScore(scene) {
    if (!scene) return;
    var all = scene.querySelectorAll(".cw-quiz");
    var checked = 0;
    var correct = 0;
    Array.prototype.forEach.call(all, function (q) {
      if (q.getAttribute("data-checked") === "1") {
        checked += 1;
        if (q.getAttribute("data-correct") === "1") correct += 1;
      }
    });
    var score = scene.querySelector("[data-quiz-score]");
    if (score) {
      score.textContent = checked ? "本次已答 " + checked + " 题，正确 " + correct + " 题" : "";
    }
  }

  function setStatus(q, text) {
    var el = q.querySelector(".cw-send-status");
    if (el) el.textContent = text;
  }

  function sendRecord(q, payload) {
    var url = META.record_url || "/api/courseware/quiz-result";
    var body;
    try {
      body = JSON.stringify(Object.assign({}, payload, {
        workspace_uuid: META.workspace_uuid || "",
        courseware: META.courseware || "",
        courseware_path: META.courseware_path || "",
        attempt_id: attemptId,
        ts: new Date().toISOString()
      }));
    } catch (e) {
      setStatus(q, "未记录");
      return;
    }

    setStatus(q, "记录中…");
    fetch(url, {
      method: "POST",
      mode: "cors",
      cache: "no-store",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: body
    }).then(function (res) {
      if (!res.ok) throw new Error("http " + res.status);
      setStatus(q, "已记录");
    }).catch(function () {
      try {
        var ok = navigator.sendBeacon && navigator.sendBeacon(
          url, new Blob([body], { type: "text/plain;charset=UTF-8" })
        );
        setStatus(q, ok ? "已提交" : "未记录");
      } catch (e) {
        setStatus(q, "未记录");
      }
    });
  }

  function selectedOf(q, type, options) {
    if (type === "fill") {
      return Array.prototype.map.call(q.querySelectorAll(".cw-fill-row input"), function (inp) {
        return normalizeText(inp.value);
      });
    }
    var picked = [];
    Array.prototype.forEach.call(q.querySelectorAll('input[type="checkbox"], input[type="radio"]'), function (inp) {
      if (inp.checked) picked.push(inp.value);
    });
    picked.sort();
    return type === "multiple" ? picked : (picked[0] || "");
  }

  function sameAnswer(type, selected, answer) {
    if (type === "fill") {
      if (!Array.isArray(answer) || !Array.isArray(selected)) return false;
      if (answer.length !== selected.length) return false;
      for (var i = 0; i < answer.length; i++) {
        if (normalizeText(answer[i]) !== normalizeText(selected[i])) return false;
      }
      return true;
    }
    if (type === "multiple") {
      if (!Array.isArray(answer) || !Array.isArray(selected)) return false;
      if (answer.length !== selected.length) return false;
      var a = answer.slice().sort();
      var s = selected.slice().sort();
      for (var j = 0; j < a.length; j++) if (normalizeText(a[j]) !== normalizeText(s[j])) return false;
      return true;
    }
    return normalizeText(selected) === normalizeText(answer);
  }

  function markOptions(q, type, answer, selected) {
    if (type === "fill") return;
    var right = Array.isArray(answer) ? answer : [answer];
    var picked = Array.isArray(selected) ? selected : [selected];
    Array.prototype.forEach.call(q.querySelectorAll(".cw-option"), function (opt) {
      var letter = opt.getAttribute("data-letter");
      opt.classList.remove("is-correct", "is-wrong");
      if (right.indexOf(letter) >= 0) opt.classList.add("is-correct");
      else if (picked.indexOf(letter) >= 0) opt.classList.add("is-wrong");
    });
  }

  function checkQuiz(q) {
    var type = q.getAttribute("data-type") || "single";
    var answer;
    var options;
    try { answer = JSON.parse(q.getAttribute("data-answer") || '""'); } catch (e) { answer = ""; }
    try { options = JSON.parse(q.getAttribute("data-options") || "[]"); } catch (e) { options = []; }

    var selected = selectedOf(q, type, options);
    var ok = sameAnswer(type, selected, answer);
    q.setAttribute("data-checked", "1");
    q.setAttribute("data-correct", ok ? "1" : "0");
    markOptions(q, type, answer, selected);

    var fb = q.querySelector(".cw-quiz-feedback");
    if (fb) {
      fb.classList.add("is-show");
      fb.classList.remove("ok", "bad");
      fb.classList.add(ok ? "ok" : "bad");
      var answerText = Array.isArray(answer) ? answer.join("、") : answer;
      fb.innerHTML = (ok ? "✓ 答对了！" : "✗ 再想想，正确答案是 " + escapeHtml(answerText)) +
        (q.getAttribute("data-explain") ? '<span class="cw-explain">' + escapeHtml(q.getAttribute("data-explain")) + "</span>" : "");
    }

    updateQuizScore(q.closest(".cw-scene"));
    sendRecord(q, {
      scene_id: q.getAttribute("data-scene") || "",
      question_id: q.getAttribute("data-qid") || "",
      question: q.getAttribute("data-question") || "",
      options: options,
      selected: selected,
      answer: answer,
      explain: q.getAttribute("data-explain") || "",
      is_correct: ok
    });
  }

  function revealQuiz(q) {
    var type = q.getAttribute("data-type") || "single";
    var answer;
    try { answer = JSON.parse(q.getAttribute("data-answer") || '""'); } catch (e) { answer = ""; }
    var answerText = Array.isArray(answer) ? answer.join("、") : answer;
    markOptions(q, type, answer, []);
    var fb = q.querySelector(".cw-quiz-feedback");
    if (fb) {
      fb.classList.add("is-show");
      fb.classList.remove("ok", "bad");
      fb.classList.add("ok");
      fb.innerHTML = "正确答案：" + escapeHtml(answerText) +
        (q.getAttribute("data-explain") ? '<span class="cw-explain">' + escapeHtml(q.getAttribute("data-explain")) + "</span>" : "");
    }
  }

  function escapeHtml(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function initQuizzes() {
    Array.prototype.forEach.call(document.querySelectorAll(".cw-quiz"), function (q) {
      var type = q.getAttribute("data-type") || "single";
      if (type !== "fill") {
        Array.prototype.forEach.call(q.querySelectorAll(".cw-option"), function (opt) {
          var input = opt.querySelector("input");
          if (!input) return;
          opt.addEventListener("click", function (ev) {
            if (ev.target !== input) input.click();
          });
          input.addEventListener("change", function () {
            if (type === "single") {
              Array.prototype.forEach.call(q.querySelectorAll(".cw-option"), function (o) {
                o.classList.toggle("is-selected", o === opt);
              });
            } else {
              opt.classList.toggle("is-selected", input.checked);
            }
          });
        });
      }
      var check = q.querySelector(".cw-check");
      if (check) check.addEventListener("click", function () { checkQuiz(q); });
      var reveal = q.querySelector(".cw-reveal");
      if (reveal) reveal.addEventListener("click", function () { revealQuiz(q); });
    });
  }

  function renderMath() {
    if (!window.katex) return;
    Array.prototype.forEach.call(document.querySelectorAll("[data-tex]"), function (el) {
      var tex = el.getAttribute("data-tex") || "";
      try {
        window.katex.render(tex, el, {
          throwOnError: false,
          displayMode: el.getAttribute("data-display") === "1",
          trust: false,
          strict: "warn"
        });
      } catch (e) {
        el.textContent = tex;
      }
    });
  }

  function bindKeys() {
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target && ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (ev.key === "ArrowRight" || ev.key === "PageDown" || ev.key === " ") {
        ev.preventDefault(); go(idx + 1);
      } else if (ev.key === "ArrowLeft" || ev.key === "PageUp") {
        ev.preventDefault(); go(idx - 1);
      } else if (ev.key === "Home") {
        ev.preventDefault(); go(0);
      } else if (ev.key === "End") {
        ev.preventDefault(); go(total - 1);
      } else if (ev.key === "n" || ev.key === "N") {
        ev.preventDefault(); toggleNotes();
      } else if (ev.key === "f" || ev.key === "F") {
        ev.preventDefault(); toggleFull();
      }
    });
  }

  function bindTouch() {
    var startX = null;
    if (!viewport) return;
    viewport.addEventListener("touchstart", function (ev) {
      if (ev.touches.length === 1) startX = ev.touches[0].clientX;
    }, { passive: true });
    viewport.addEventListener("touchend", function (ev) {
      if (startX == null || !ev.changedTouches.length) return;
      var dx = ev.changedTouches[0].clientX - startX;
      startX = null;
      if (Math.abs(dx) < 50) return;
      go(idx + (dx < 0 ? 1 : -1));
    }, { passive: true });
  }

  function boot() {
    scaleStage();
    go(0);
    initQuizzes();
    renderMath();
    bindKeys();
    bindTouch();

    var prev = document.getElementById("cwPrevBtn");
    var next = document.getElementById("cwNextBtn");
    var full = document.getElementById("cwFullBtn");
    var close = document.getElementById("cwNotesClose");
    if (prev) prev.addEventListener("click", function () { go(idx - 1); });
    if (next) next.addEventListener("click", function () { go(idx + 1); });
    if (full) full.addEventListener("click", toggleFull);
    if (notesBtn) notesBtn.addEventListener("click", function () { toggleNotes(); });
    if (close) close.addEventListener("click", function () { toggleNotes(false); });

    window.addEventListener("resize", scaleStage);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scaleStage).catch(function () {});
    window.cwGo = go;
    window.cwNotes = toggleNotes;
    window.cwState = { index: function () { return idx; }, total: total };
    window.cwReady = true;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
