/* ragapp/static/ragapp/javascript/news.js
   2025-11-18 웹 검색 + RAG 검색용 JS (새 화면 구성까지 담당)
   - 동의/화면 흐림(블러) 처리는 news.html 안의 인라인 스크립트에서 다룹니다.
   - 이 파일에서는:
     · 자잘한 도우미 함수
     · 입력 폼(질문창) 처리
     · 웹 / RAG 답변을 화면에 예쁘게 넣어 주기
     · 서버에 검색 요청 보내기(AJAX)
     · 상단 햄버거 메뉴·푸터 모양 잡기
     를 담당합니다.
*/

/* ---------- 작은 도우미들 ---------- */
function escHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function stripTrailingColon(t) {
  return (t || "").replace(/:\s*$/, "").trim();
}

function cleanLeading(id) {
  var el = document.getElementById(id);
  if (!el) return;
  var h = el.innerHTML;
  if (!h) return;
  el.innerHTML = h
    .replace(/^(<br\s*\/?>\s*)+/i, "")
    .replace(/^\s+/, "");
}

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

function setCookie(name, value, days) {
  try {
    const maxAge = days ? "; max-age=" + days * 24 * 60 * 60 : "";
    document.cookie =
      name + "=" + encodeURIComponent(value) + "; path=/" + maxAge;
  } catch (e) { }
}

/* ---------- HTML 정리 ---------- */
function sanitizeHTML(unsafe) {
  try {
    const ALLOWED = new Set([
      "B",
      "I",
      "STRONG",
      "EM",
      "BR",
      "UL",
      "OL",
      "LI",
      "P",
      "CODE",
      "PRE",
      "A",
    ]);
    const ALLOWED_ATTR = new Set(["href", "target", "rel"]);
    const T = document.createElement("template");
    T.innerHTML = unsafe || "";

    const walk = function (n) {
      var children = Array.from(n.childNodes);
      for (var i = 0; i < children.length; i++) {
        var c = children[i];
        if (c.nodeType === 1) {
          // 태그(Element)인 경우
          if (!ALLOWED.has(c.tagName)) {
            // 허용되지 않은 태그는 안쪽 내용만 남기고 태그는 제거
            while (c.firstChild) {
              c.parentNode.insertBefore(c.firstChild, c);
            }
            c.remove();
            continue;
          }
          if (c.tagName === "A") {
            Array.from(c.attributes).forEach(function (a) {
              if (!ALLOWED_ATTR.has(a.name.toLowerCase())) {
                c.removeAttribute(a.name);
              }
            });
            var href = c.getAttribute("href") || "";
            if (!/^https?:\/\//i.test(href)) {
              c.removeAttribute("href");
            }
            c.setAttribute("rel", "noopener noreferrer");
            c.setAttribute("target", "_blank");
          } else {
            Array.from(c.attributes).forEach(function (a) {
              c.removeAttribute(a.name);
            });
          }
          walk(c);
        } else if (c.nodeType === 8) {
          // 주석은 제거
          c.remove();
        }
      }
    };

    walk(T.content || T);

    var root = T.content || T;
    return root.firstChild ? T.innerHTML : T.innerHTML || "";
  } catch (e) {
    return escHtml(unsafe || "");
  }
}

/* ---------- 전송 상태 표시 ---------- */
/** 버튼에 "⏳ 처리 중..." 같은 문구를 잠깐 보여 주고, 중복 클릭을 막는다. */
function setLoading(formEl) {
  try {
    var hiddenAction = formEl.querySelector('input[name="action"]');
    var submitter = document.activeElement;

    if (submitter && submitter.tagName === "BUTTON") {
      var a = submitter.getAttribute("data-action");
      if (a && hiddenAction) {
        hiddenAction.value = a;
      }

      submitter.disabled = true;
      submitter.dataset.origText = submitter.innerText || submitter.value || "";

      if (submitter.innerText !== undefined) {
        submitter.innerText = "⏳ 처리 중...";
      } else if (submitter.value !== undefined) {
        submitter.value = "⏳ 처리 중...";
      }
    }
  } catch (e) { }
  return true;
}

// 전역에서도 사용 가능(폼 onsubmit 에서 window.setLoading(...) 형태로 사용)
if (typeof window !== "undefined") {
  window.setLoading = window.setLoading || setLoading;
}

/* ---------- 웹 요약 블럭 정리 ---------- */
function transformWebAnswerBlock() {
  var el = document.getElementById("web-answer-block");
  if (!el) return;

  var raw = el.innerHTML || "";
  if (!raw || !raw.trim()) return;

  var lines = raw.split(/<br\s*\/?>/i);
  var out = [];

  for (var i = 0; i < lines.length; i++) {
    var t = (lines[i] || "").trim();
    if (!t) {
      out.push("");
      continue;
    }

    if (/^\(https?:\/\/[^\)]+\)\s*$/i.test(t)) continue;

    var mA = t.match(
      /^(\d+\.\s*)?\*\*([^*]+)\*\*\s*([^:]+):\s*\[([^\]]+)\]\((https?:\/\/[^\)]+)\)(.*)$/
    );
    if (mA) {
      var num = mA[1] || "";
      var label = stripTrailingColon(mA[2].trim()) + ": " + mA[3].trim();
      var url = (mA[5] || mA[4]).trim();
      var tail = mA[6] || "";
      out.push(
        escHtml(num) +
        '<a href="' +
        escHtml(url) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(label) +
        "</a>" +
        (tail ? " " + escHtml(tail) : "")
      );
      continue;
    }

    var mB = t.match(
      /^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[([^\]]+)\]\((https?:\/\/[^\)]+)\)(.*)$/
    );
    if (mB) {
      var num2 = mB[1] || "";
      var label2 = mB[2].trim();
      var url2 = (mB[4] || mB[3]).trim();
      var tail2 = mB[5] || "";
      out.push(
        escHtml(num2) +
        '<a href="' +
        escHtml(url2) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(stripTrailingColon(label2)) +
        "</a>" +
        (tail2 ? " " + escHtml(tail2) : "")
      );
      continue;
    }

    var mC = t.match(/^(\d+\.\s*)?\*\*([^*]+)\*\*\s*\[([^\]]+)\]\s*$/);
    if (mC) {
      var num3 = mC[1] || "";
      var srcOnly = mC[2].trim();
      var urlOnly = mC[3].trim();
      out.push(
        escHtml(num3) +
        '<a href="' +
        escHtml(urlOnly) +
        '" target="_blank" rel="noopener noreferrer" class="src-title">' +
        escHtml(stripTrailingColon(srcOnly)) +
        "</a>"
      );
      continue;
    }

    out.push(escHtml(t));
  }

  el.innerHTML = out.join("<br />");
}

/* ---------- DOM 준비 시 : 처음 텍스트 정리 ---------- */
document.addEventListener("DOMContentLoaded", function () {
  cleanLeading("rag-answer-block");
  cleanLeading("web-answer-block");
  transformWebAnswerBlock();
});

/* ============================================================
 *  아래부터는 "웹 검색 / RAG 검색" AJAX + 자료 저장용 서버 호출
 * ============================================================ */
(function () {
  "use strict";

  const log = function (tag, data) {
    try {
      if (typeof window !== "undefined" && typeof window.dglog === "function") {
        window.dglog("NEWS_AJAX " + tag, data);
      } else {
        const ts = new Date().toISOString().slice(11, 23);
        console.log("[news-ajax " + ts + "] " + tag, data ?? "");
      }
    } catch (_) { }
  };

  const $ = (s, r = document) => r.querySelector(s);

  // ---- 공통 POST(JSON) 도우미 ----
  function apiPostJSON(url, payload) {
    const headers = {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    };
    try {
      const csrftoken =
        typeof getCookie === "function" ? getCookie("csrftoken") : null;
      if (csrftoken) headers["X-CSRFToken"] = csrftoken;
    } catch (_) { }

    try {
      if (
        typeof window !== "undefined" &&
        typeof window.newReqId === "function"
      ) {
        const reqId = window.newReqId("ui");
        if (reqId) headers["X-Request-Id"] = reqId;
      }
    } catch (_) { }

    return fetch(url, {
      method: "POST",
      headers,
      credentials: "same-origin",
      body: JSON.stringify(payload || {}),
    }).then(async (res) => {
      const text = await res.text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch (_) { }

      if (!res.ok || (json && json.ok === false)) {
        const msg =
          (json && (json.error || json.message || json.detail)) ||
          "HTTP " + res.status;
        const err = new Error(msg);
        err.response = json || text;
        throw err;
      }
      return json || {};
    });
  }

  // ---- 응답에서 자주 쓰는 필드 꺼내기 ----
  function pickAnswer(j) {
    try {
      if (!j) return "";
      const keys = ["answer_text", "answer", "text", "reply", "result", "a", "data"];
      for (const k of keys) {
        if (typeof j[k] === "string" && j[k].trim()) return j[k];
      }
    } catch (_) { }
    return "";
  }

  function pickSources(j) {
    try {
      if (!j) return [];
      const cand =
        j.sources || j.web_sources || j.hits || j.docs || j.references || [];
      return Array.isArray(cand) ? cand : [];
    } catch (_) { }
    return [];
  }

  function pickLogId(j) {
    try {
      if (!j) return "";
      const keys = ["log_id", "id", "logId"];
      for (const k of keys) {
        if (j[k] !== undefined && j[k] !== null) return String(j[k]);
      }
    } catch (_) { }
    return "";
  }

  function pickMsg(j) {
    try {
      if (!j) return "";
      return j.msg || j.message || "";
    } catch (_) { }
    return "";
  }

  // ---- 화면에 텍스트 / 출처 목록 넣기 ----
  function renderTextWithBR(target, text) {
    if (!target) return;
    try {
      const html = escHtml(String(text || "")).replace(
        /\r\n|\r|\n/g,
        "<br/>"
      );
      target.innerHTML = html;
    } catch (e) {
      target.textContent = String(text || "");
    }
  }

  function renderSourcesList(containerUl, sources) {
    try {
      if (!containerUl) return;
      const block = containerUl.closest(".sources-block");
      containerUl.innerHTML = "";

      if (!Array.isArray(sources) || sources.length === 0) {
        if (block) block.setAttribute("hidden", "hidden");
        return;
      }
      if (block) block.removeAttribute("hidden");

      sources.forEach((src) => {
        try {
          const li = document.createElement("li");
          const title =
            (src && (src.title || src.name || src.url)) || "(제목 없음)";
          const url = src && src.url;

          if (url) {
            const a = document.createElement("a");
            a.className = "src-title";
            a.href = url;
            a.target = "_blank";
            a.rel = "noopener noreferrer nofollow";
            a.referrerPolicy = "strict-origin-when-cross-origin";
            a.textContent = title || url;
            li.appendChild(a);
          } else {
            const span = document.createElement("span");
            span.className = "src-title";
            span.textContent = title;
            li.appendChild(span);
          }
          containerUl.appendChild(li);
        } catch (_) { }
      });
    } catch (e) {
      log("RENDER_SOURCES_ERR", e && e.message ? e.message : e);
    }
  }

  function updateFeedbackRow(rowSelector, payload) {
    try {
      const row = document.querySelector(rowSelector);
      if (!row) return;

      if (payload.question) row.dataset.question = String(payload.question);
      if (payload.answer) row.dataset.answer = String(payload.answer);
      if (Array.isArray(payload.sources)) {
        try {
          row.dataset.sources = JSON.stringify(payload.sources);
        } catch (_) { }
      }
      if (payload.logId) row.dataset.logId = String(payload.logId);

      const st = row.querySelector(".fb-status");
      if (st) st.textContent = "";
    } catch (e) {
      log("FB_DATASET_ERR", e && e.message ? e.message : e);
    }
  }

  function restoreSubmitter(ev, form) {
    try {
      const submitter =
        (ev && ev.submitter) ||
        form.querySelector("button[disabled][data-orig-text]");
      if (!submitter) return;
      submitter.disabled = false;
      if (submitter.dataset && submitter.dataset.origText) {
        if (submitter.innerText !== undefined) {
          submitter.innerText = submitter.dataset.origText;
        } else if (submitter.value !== undefined) {
          submitter.value = submitter.dataset.origText;
        }
        delete submitter.dataset.origText;
      }
    } catch (_) { }
  }

  // ---- 웹 검색 폼: /api/web_qa ----
  function setupWebForm() {
    try {
      const input = $("#query_web");
      if (!input) return;
      const form = input.closest("form");
      if (!form) return;

      const searchBtn = form.querySelector('button[data-action="web_search"]');
      const ingestBtn = form.querySelector('button[data-action="web_ingest"]');

      // 기존 onclick(동의 관련 JS 등) 제거
      if (searchBtn) {
        try {
          searchBtn.onclick = null;
          searchBtn.removeAttribute("onclick");
        } catch (_) { }
      }
      if (ingestBtn) {
        try {
          ingestBtn.onclick = null;
          ingestBtn.removeAttribute("onclick");
        } catch (_) { }
      }

      // 실제 웹 검색 실행 (AJAX)
      function runWeb(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }
          const query = String(input.value || "").trim();
          if (!query) return;

          const ansBlock = document.getElementById("web-answer-block");
          const card = ansBlock && ansBlock.closest(".card");
          const msgRow = card && card.querySelector(".msg-row");
          const srcUl = document.getElementById("webSourcesList");

          if (msgRow) msgRow.innerHTML = "";
          if (ansBlock) renderTextWithBR(ansBlock, "웹에서 내용을 정리하는 중입니다…");
          if (srcUl) {
            srcUl.innerHTML = "";
            const srcBlock = srcUl.closest(".sources-block");
            if (srcBlock) srcBlock.setAttribute("hidden", "hidden");
          }

          apiPostJSON("/api/web_qa", {
            q: query,
            query: query,
            question: query,
          })
            .then(function (j) {
              const ans = pickAnswer(j) || "(받아온 답이 없습니다.)";
              const srcs = pickSources(j);
              const msg = pickMsg(j);
              const logId = pickLogId(j);

              if (msgRow) {
                msgRow.innerHTML = msg
                  ? '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>"
                  : "";
              }
              renderTextWithBR(ansBlock, ans);
              renderSourcesList(srcUl, srcs);

              updateFeedbackRow(
                '.main-feedback-row[data-answer-type="gemini"]',
                {
                  question: query,
                  answer: ans,
                  sources: srcs,
                  logId: logId,
                }
              );
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "웹에서 답을 만드는 동안 문제가 발생했습니다.";
              log("WEB_QA_ERR", m);
              const card2 = document
                .getElementById("web-answer-block")
                ?.closest(".card");
              const msgRow2 = card2 && card2.querySelector(".msg-row");
              if (msgRow2) {
                msgRow2.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("WEB_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      // 💗 웹 검색 결과를 "나중에 다시 쓸 수 있도록" 저장 → /api/rag/upsert
      function runWebIngest(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }

          const query = String(input.value || "").trim();
          if (!query) {
            alert("먼저 궁금한 내용을 적어 주세요.");
            return;
          }

          const ansBlock = document.getElementById("web-answer-block");
          const answer = ansBlock
            ? String(ansBlock.textContent || "").trim()
            : "";

          if (!answer) {
            alert(
              '먼저 "웹에서 검색"을 눌러 답변을 만든 다음, 저장 버튼을 눌러 주세요.'
            );
            return;
          }

          const srcUl = document.getElementById("webSourcesList");
          const sources = [];
          if (srcUl) {
            srcUl.querySelectorAll("li").forEach(function (li) {
              try {
                const a = li.querySelector("a");
                if (!a) return;
                const url = a.getAttribute("href") || "";
                const title = a.textContent || url;
                if (!url) return;
                sources.push({
                  url: url,
                  title: title,
                });
              } catch (_) { }
            });
          }

          const card = ansBlock && ansBlock.closest(".card");
          const msgRow = card && card.querySelector(".msg-row");
          if (msgRow) {
            msgRow.innerHTML =
              '<div class="msg-ok" role="status">⏳ 웹에서 찾은 내용을 나중에 다시 쓸 수 있도록 저장하는 중입니다…</div>';
          }

          const payload = {
            question: query,
            answer: answer,
            sources: sources,
            answer_type: "web",
            from_ui: "news_web_panel",
          };

          apiPostJSON("/api/rag/upsert", payload)
            .then(function (j) {
              const msg =
                (j && (j.msg || j.message)) ||
                "웹에서 찾은 내용을 잘 저장해 두었습니다.";
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>";
              }
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "웹에서 찾은 내용을 저장하는 동안 문제가 발생했습니다.";
              log("WEB_INGEST_ERR", m);
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("WEB_INGEST_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      // 🔵 웹 검색 버튼 → AJAX
      if (searchBtn) {
        searchBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "web_search";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runWeb(ev);
        });
      }

      // 💗 "웹 결과 저장" 버튼 → /api/rag/upsert (AJAX)
      if (ingestBtn) {
        ingestBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "web_ingest";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runWebIngest(ev);
        });
      }

      // Enter 로 제출할 때는 웹 검색만 JS로 처리
      form.addEventListener("submit", function (ev) {
        try {
          const hidden = form.querySelector('input[name="action"]');
          const action = (hidden && hidden.value) || "web_search";
          const query = String(input.value || "").trim();

          if (action === "web_search" && query) {
            runWeb(ev);
          }
        } catch (e) {
          log("WEB_FORM_HANDLER_ERR", e && e.message ? e.message : e);
        }
      });
    } catch (e) {
      log("WEB_FORM_SETUP_ERR", e && e.message ? e.message : e);
    }
  }

  // ---- RAG 검색 폼: /api/rag_qa ----
  function setupRagForm() {
    try {
      const input = document.querySelector('input[name="query_rag"]');
      if (!input) return;
      const form = input.closest("form");
      if (!form) return;

      const ragBtn = form.querySelector('button[data-action="rag_search"]');
      const seedBtn = form.querySelector('button[data-action="rag_seed"]');
      const resetBtn = form.querySelector('button[data-action="rag_reset"]');

      // 기존 onclick 제거 (동의 관련 JS 등)
      [ragBtn, seedBtn, resetBtn].forEach(function (btn) {
        if (!btn) return;
        try {
          btn.onclick = null;
          btn.removeAttribute("onclick");
        } catch (_) { }
      });

      // 실제 RAG 검색 실행
      function runRag(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }
          const query = String(input.value || "").trim();
          if (!query) return;

          const msgRow = document.getElementById("rag-msg-block");
          const ansBlock = document.getElementById("rag-answer-block");

          if (msgRow) msgRow.innerHTML = "";
          if (ansBlock)
            renderTextWithBR(
              ansBlock,
              "자료를 모아서 답을 만드는 중입니다…"
            );

          apiPostJSON("/api/rag_qa", {
            q: query,
            query: query,
            question: query,
          })
            .then(function (j) {
              const ans = pickAnswer(j) || "(받아온 답이 없습니다.)";
              const srcs = pickSources(j);
              const msg = pickMsg(j);
              const logId = pickLogId(j);

              if (msgRow) {
                msgRow.innerHTML = msg
                  ? '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>"
                  : "";
              }
              renderTextWithBR(ansBlock, ans);

              updateFeedbackRow(
                '.main-feedback-row[data-answer-type="rag"]',
                {
                  question: query,
                  answer: ans,
                  sources: srcs,
                  logId: logId,
                }
              );
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "답을 만드는 동안 문제가 발생했습니다.";
              log("RAG_QA_ERR", m);
              const msgRow2 = document.getElementById("rag-msg-block");
              if (msgRow2) {
                msgRow2.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("RAG_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      // 🧱 기본 자료 채워 넣기(시드 업서트) 실행 (AJAX, GET /api/rag/seed)
      function runRagSeed(ev) {
        try {
          if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          }

          const query = String(input.value || "").trim();
          const msgRow = document.getElementById("rag-msg-block");
          const ansBlock = document.getElementById("rag-answer-block");

          if (msgRow) {
            msgRow.innerHTML =
              '<div class="msg-ok" role="status">⏳ 기본 자료를 채워 넣는 중입니다…</div>';
          }
          if (ansBlock) {
            renderTextWithBR(
              ansBlock,
              "기본 자료를 채워 넣고 있습니다. 잠시만 기다려 주세요…"
            );
          }

          const params = new URLSearchParams();
          params.set("from_ui", "news_rag_panel");
          if (query) {
            params.set("last_query", query);
          }

          const url = "/api/rag/seed?" + params.toString();

          fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: {
              "X-Requested-With": "XMLHttpRequest",
            },
          })
            .then(async (res) => {
              const text = await res.text();
              let j = null;
              try {
                j = JSON.parse(text);
              } catch (_) { }

              if (!res.ok || (j && j.ok === false)) {
                const msgErr =
                  (j && (j.error || j.message || j.detail)) ||
                  text ||
                  "기본 자료를 채우는 중에 문제가 발생했습니다.";
                const err = new Error(msgErr);
                err.response = j || text;
                throw err;
              }

              const msg =
                (j && (j.msg || j.message)) ||
                "기본 자료 채우기가 끝났습니다.";
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-ok" role="status">✅ ' +
                  escHtml(msg) +
                  "</div>";
              }
              if (ansBlock) {
                renderTextWithBR(
                  ansBlock,
                  "기본 자료 채우기가 끝났습니다. 이제 검색 창에서 잘 나오는지 시험해 보세요!"
                );
              }
            })
            .catch(function (err) {
              const m =
                (err && err.message) ||
                "기본 자료를 채우는 중에 문제가 발생했습니다.";
              log("RAG_SEED_ERR", m);
              if (msgRow) {
                msgRow.innerHTML =
                  '<div class="msg-err" role="alert">❌ ' +
                  escHtml(m) +
                  "</div>";
              }
            })
            .finally(function () {
              if (ev) restoreSubmitter(ev, form);
            });
        } catch (e) {
          log("RAG_SEED_RUN_ERR", e && e.message ? e.message : e);
        }
      }

      // 🧱 기본 자료 채우기 버튼 → AJAX
      if (seedBtn) {
        seedBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_seed";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runRagSeed(ev);
        });
      }

      // 🤖 RAG 검색 버튼 → AJAX
      if (ragBtn) {
        ragBtn.addEventListener("click", function (ev) {
          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_search";
          } catch (_) { }
          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }
          runRag(ev);
        });
      }

      // 🗑 DB 초기화 버튼 → 서버로 직접 폼 POST
      if (resetBtn) {
        resetBtn.addEventListener("click", function (ev) {
          try {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          } catch (_) { }

          try {
            const hidden = form.querySelector('input[name="action"]');
            if (hidden) hidden.value = "rag_reset";
          } catch (_) { }

          try {
            if (typeof setLoading === "function") setLoading(form);
          } catch (_) { }

          try {
            form.submit();
          } catch (e) {
            log("RAG_RESET_SUBMIT_ERR", e && e.message ? e.message : e);
          }
        });
      }

      // Enter 로 제출 → 기본적으로 RAG 검색만 AJAX로 처리
      form.addEventListener("submit", function (ev) {
        try {
          const hidden = form.querySelector('input[name="action"]');
          const action = (hidden && hidden.value) || "rag_search";
          const query = String(input.value || "").trim();

          if (action === "rag_search" && query) {
            runRag(ev);
          }
        } catch (e) {
          log("RAG_FORM_HANDLER_ERR", e && e.message ? e.message : e);
        }
      });
    } catch (e) {
      log("RAG_FORM_SETUP_ERR", e && e.message ? e.message : e);
    }
  }

  // ---- 초기 설정 ----
  document.addEventListener("DOMContentLoaded", function () {
    try {
      setupWebForm();
      setupRagForm();
      log("INIT_DONE", {});
    } catch (e) {
      log("DOM_READY_ERR", e && e.message ? e.message : e);
    }
  });
})();

/* ============================================================
 *  새 화면 전용 JS: 햄버거 메뉴 + 테스터 안내 바 + 법무 푸터
 * ============================================================ */
(function () {
  "use strict";

  function setupMmHamburger() {
    var nav = document.querySelector(".page-header .nav-links");
    if (!nav) return;
    if (nav.dataset.mmReady === "1") return;
    nav.dataset.mmReady = "1";

    // 햄버거 버튼 생성 (질문 챗봇(QARAG) 옆에 붙는 버튼)
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn mm-toggle-btn";
    btn.innerHTML =
      '<span class="mm-toggle-btn-icon">☰</span><span>도구 모음</span>';
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");

    // 위에서 눌렀을 때 열리는 작은 메뉴
    var pop = document.createElement("div");
    pop.className = "mm-popover";
    pop.setAttribute("role", "menu");
    pop.hidden = true;
    pop.innerHTML = [
      '<div class="mm-popover-inner">',
      '  <div class="mm-popover-title">도구 및 도움말</div>',
      '  <div class="mm-popover-desc">이미지 · 표 · 파일을 올리고 찾는 방법을 한곳에 모아 둔 메뉴입니다.</div>',
      '  <div class="mm-popover-items">',
      '    <button type="button" class="mm-pill" data-mm="index">📚 자료 올려두기 안내</button>',
      '    <button type="button" class="mm-pill" data-mm="image">🖼 글로 사진 찾기 안내</button>',
      '    <button type="button" class="mm-pill" data-mm="csv">📊 표 파일(CSV) 올리기 안내</button>',
      '    <button type="button" class="mm-pill" data-mm="table">📋 표 내용 검색 안내</button>',
      "  </div>",
      "</div>",
    ].join("");

    nav.insertBefore(btn, nav.firstChild);
    nav.appendChild(pop);

    var open = false;

    function closeMenu() {
      pop.hidden = true;
      pop.removeAttribute("data-open");
      btn.setAttribute("aria-expanded", "false");
      open = false;
    }

    function openMenu() {
      pop.hidden = false;
      pop.setAttribute("data-open", "1");
      btn.setAttribute("aria-expanded", "true");
      open = true;
    }

    function toggleMenu() {
      if (open) closeMenu();
      else openMenu();
    }

    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      toggleMenu();
    });

    document.addEventListener("click", function (ev) {
      if (!open) return;
      if (pop.contains(ev.target) || btn.contains(ev.target)) return;
      closeMenu();
    });

    document.addEventListener("keydown", function (ev) {
      if (!open) return;
      if (ev.key === "Escape") {
        closeMenu();
        btn.focus();
      }
    });

    // 각 메뉴를 눌렀을 때는 일단 로그만 남겨 두고,
    // 실제 페이지 이동은 나중에 필요할 때 연결한다.
    pop.addEventListener("click", function (ev) {
      var pill = ev.target.closest(".mm-pill");
      if (!pill) return;
      var kind = pill.getAttribute("data-mm") || "";

      try {
        if (typeof window.dglog === "function") {
          window.dglog("MM_TOOL_CLICK", { kind: kind });
        }
      } catch (e) { }

      // TODO: 나중에 필요하면 여기서 location.href = "..."; 등으로 실제 이동 처리
      closeMenu();
    });
  }

  function prettifyTesterAndLegal() {
    // 본문 안에 있는 "테스터 고지 ..." / "개인정보처리방침 ..." 문장을 찾아서
    // 예쁜 안내 바와 하단 링크로 다시 배치한다.
    var allPs = document.querySelectorAll(
      "body > p, body > div > p, body > section > p"
    );
    var testerP = null;
    var legalP = null;

    allPs.forEach(function (p) {
      var text = (p.textContent || "").trim();
      if (!testerP && text.indexOf("테스터 고지") === 0) {
        testerP = p;
      }
      if (!legalP && text.indexOf("개인정보처리방침") !== -1) {
        legalP = p;
      }
    });

    // 테스터 안내 바
    if (testerP) {
      var wrap = document.createElement("section");
      wrap.className = "tester-strip";

      var inner = document.createElement("div");
      inner.className = "tester-strip-inner";

      var badge = document.createElement("span");
      badge.className = "tester-badge";
      badge.textContent = "테스터 안내";

      var textEl = document.createElement("p");
      textEl.className = "tester-text";

      // 앞쪽 "테스터 고지"라는 문구는 배지로 빼고, 나머지 문장만 내용으로 쓴다.
      var html = testerP.innerHTML.replace(/^\s*테스터 고지\s*/i, "");
      textEl.innerHTML = html;

      inner.appendChild(badge);
      inner.appendChild(textEl);
      wrap.appendChild(inner);

      testerP.replaceWith(wrap);
    }

    // 하단 약관/정책 링크 푸터
    if (legalP) {
      var footer = document.createElement("footer");
      footer.className = "site-footer";

      var innerF = document.createElement("div");
      innerF.className = "site-footer-inner";

      var nav = document.createElement("nav");
      nav.className = "site-footer-links";
      nav.setAttribute("aria-label", "약관 및 안내 링크");

      // 기존 p 안에 있던 링크/텍스트 그대로 옮겨 담기
      nav.innerHTML = legalP.innerHTML;

      innerF.appendChild(nav);
      footer.appendChild(innerF);
      legalP.replaceWith(footer);
    }
  }

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    try {
      setupMmHamburger();
    } catch (e) {
      console.error(
        "[mm hamburger]",
        e && e.message ? e.message : e
      );
    }

    try {
      prettifyTesterAndLegal();
    } catch (e) {
      console.error(
        "[tester/footer layout]",
        e && e.message ? e.message : e
      );
    }
  });
})();
