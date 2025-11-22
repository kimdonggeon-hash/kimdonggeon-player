/* ragapp/static/ragapp/javascript/livechat_client.js */
/* QARAG ↔ 실시간 상담 콘솔 WebSocket 클라이언트 (브라우저 측)
   + 질문 챗봇(QARAG) 쪽 상담 연결/종료 UI 로직 통합
*/
(function () {
    "use strict";

    try {
        // ─────────────────────────────────────────────────────────────
        // 경량 로거
        // ─────────────────────────────────────────────────────────────
        const log = (tag, data) => {
            try {
                if (typeof window.dglog === "function") {
                    window.dglog(tag, data);
                } else {
                    const ts = new Date().toISOString().slice(11, 23);
                    console.log(`[livechat ${ts}] ${tag}`, data ?? "");
                }
            } catch (_) { }
        };

        // ─────────────────────────────────────────────────────────────
        // 상수 / 유틸
        // ─────────────────────────────────────────────────────────────
        const STORAGE_KEY = "live_chat_room_id";

        const scheme = () => (location.protocol === "https:" ? "wss" : "ws");
        const mkURL = (path) => `${scheme()}://${location.host}${path}`;

        // 운영자(마스터 콘솔) 브로드캐스트용 후보 URL
        const lobbyCandidates = () => {
            const base = "/ws/chat/master";
            return [mkURL(base), mkURL(base + "/")];
        };

        // 사용자 개별 방 URL 후보
        const roomCandidates = (room) => {
            const base = `/ws/chat/${encodeURIComponent(room)}`;
            return [mkURL(base), mkURL(base + "/")];
        };

        // 브라우저 로컬에 고정된 room id (페이지 새로고침해도 유지)
        const getRoomId = () => {
            try {
                let r = localStorage.getItem(STORAGE_KEY);
                if (!r) {
                    r =
                        "client-" +
                        Math.random().toString(36).slice(2, 8) +
                        "-" +
                        Date.now().toString(36);
                    localStorage.setItem(STORAGE_KEY, r);
                }
                return r;
            } catch (_) {
                return (
                    "client-" +
                    Math.random().toString(36).slice(2, 8) +
                    "-" +
                    Date.now().toString(36)
                );
            }
        };

        // WebSocket payload 안전 파서
        const safeParse = (raw) => {
            try {
                let j = typeof raw === "string" ? JSON.parse(raw) : raw || {};
                if (j && typeof j === "object") {
                    // message/msg/data/text 안에 JSON 문자열이 한 번 더 싸여 있는 경우도 처리
                    for (const k of ["message", "msg", "data", "text"]) {
                        if (typeof j[k] === "string") {
                            try {
                                const jj = JSON.parse(j[k]);
                                if (jj && typeof jj === "object") {
                                    j = Object.assign({}, j, jj);
                                }
                            } catch (_) { }
                        }
                    }
                }
                return j;
            } catch (_) {
                return { sender: "system", text: String(raw ?? "") };
            }
        };

        const $ = (s, r = document) => r.querySelector(s);

        // QARAG 메시지 추가 (공용 헬퍼 있으면 우선 사용)
        function pushMsg(role, text) {
            try {
                if (typeof window.__qaragAddMsg === "function") {
                    return window.__qaragAddMsg(role, text);
                }
            } catch (_) { }

            const box = $("#qaragMessages");
            if (!box) return;

            const wrap = document.createElement("div");
            wrap.className = "qarag-msgwrap " + (role === "user" ? "user" : "bot");

            const div = document.createElement("div");
            div.className = "qarag-msg " + (role === "user" ? "user" : "bot");
            div.textContent = String(text || "");

            wrap.appendChild(div);
            box.appendChild(wrap);
            box.scrollTop = box.scrollHeight;
        }

        // QARAG 패널 열기 (있으면)
        const openPanel = () => {
            try {
                if (typeof window.openQaragPanel === "function") {
                    window.openQaragPanel();
                    return;
                }
                const panel = $("#qaragPanel");
                if (panel) panel.classList.add("show");
            } catch (_) { }
        };

        // ─────────────────────────────────────────────────────────────
        // QARAG 상태 / DOM 레퍼런스
        // ─────────────────────────────────────────────────────────────
        const g = window;
        g.__qaragState = g.__qaragState || {};
        if (typeof g.__qaragState.liveEnded === "undefined")
            g.__qaragState.liveEnded = false;
        if (typeof g.__qaragState.liveSessionCode === "undefined")
            g.__qaragState.liveSessionCode = null;
        if (typeof g.__qaragState.liveSessionId === "undefined")
            g.__qaragState.liveSessionId = null;
        if (typeof g.__qaragState.operatorJoined === "undefined")
            g.__qaragState.operatorJoined = false;

        let btnConnectLive = null;
        let btnEndLive = null;
        let overlay = null;
        let agreeBtn = null;
        let cancelBtn = null;
        let msgBox = null;
        let inputBox = null;
        let sendBtn = null;

        function hideEndButton() {
            if (!btnEndLive) return;
            btnEndLive.hidden = true;
            btnEndLive.disabled = true;
            btnEndLive.style.display = "none";
        }

        function showEndButton() {
            if (!btnEndLive) return;
            btnEndLive.hidden = false;
            btnEndLive.disabled = false;
            btnEndLive.style.display = "";
        }

        function setEndedUI(reasonText) {
            const st = (g.__qaragState = g.__qaragState || {});
            st.liveEnded = true;

            hideEndButton();

            if (btnConnectLive) {
                btnConnectLive.disabled = true;
            }
            if (inputBox) {
                inputBox.disabled = true;
                inputBox.placeholder =
                    "상담이 종료되었습니다. 새 상담을 원하시면 페이지를 새로고침해 주세요.";
            }
            if (sendBtn) {
                sendBtn.disabled = true;
            }

            const msg =
                reasonText ||
                "상담이 종료되었습니다. 이용해 주셔서 감사합니다.";

            if (msg) {
                const box = msgBox || $("#qaragMessages");
                if (box && !box.dataset.liveEndedMsgShown) {
                    pushMsg("bot", msg);
                    box.dataset.liveEndedMsgShown = "1";
                }
            }
        }

        /**
         * 서버에서 들어오는 메시지에 대해
         * - 상담사 입장 감지 → 종료 버튼 노출
         * - end/closed 신호 → UI 종료 처리
         * - 어드민용 "사용자가 종료 버튼을 눌렀습니다" 문구는 숨김
         * 반환값: true 이면 기본 말풍선 렌더링은 건너뜀
         */
        function handleInboundForQarag(data) {
            try {
                const st = (g.__qaragState = g.__qaragState || {});
                const sender = String(
                    data.sender || data.role || data.from || data.type || ""
                ).toLowerCase();
                const type = String(data.type || "").toLowerCase();
                const rawText = data.text || data.message || data.msg || "";
                const body = String(rawText || "").trim();

                if (st.liveEnded) {
                    // 이미 종료된 상태에서 오는 end/closed 신호는 UI만 한 번 더 정리
                    if (type === "end" || type === "closed") {
                        setEndedUI("상담이 종료되었습니다. 이용해 주셔서 감사합니다.");
                    }
                    return true; // 종료 이후에는 추가 말풍선 표시 안 함
                }

                // 상담사가 한 번이라도 말하면 → 종료 버튼 노출
                if (sender === "operator") {
                    st.operatorJoined = true;
                    st.liveEnded = false;
                    showEndButton();
                }

                // 서버에서 오는 종료 신호
                if (type === "end" || type === "closed") {
                    setEndedUI("상담이 종료되었습니다. 이용해 주셔서 감사합니다.");
                    return true;
                }

                // 어드민 콘솔용 안내 문구는 사용자 화면에서는 숨김
                if (body === "[사용자]가 상담 종료 버튼을 눌렀습니다.") {
                    return true;
                }

                return false;
            } catch (_) {
                return false;
            }
        }

        // ─────────────────────────────────────────────────────────────
        // WebSocket 연결 헬퍼 (후보 URL들 순차 시도)
        // ─────────────────────────────────────────────────────────────
        function connectWithFallback(urls, handlers) {
            let idx = 0;
            let ws = null;
            let stopped = false;

            const tryNext = () => {
                if (stopped) return;
                if (idx >= urls.length) {
                    handlers.onerror && handlers.onerror(new Error("no candidate matched"));
                    return;
                }
                const url = urls[idx++];
                log("WS CONNECT →", url);
                try {
                    ws = new WebSocket(url);
                } catch (e) {
                    log("ERR WS NEW", String(e));
                    setTimeout(tryNext, 200);
                    return;
                }
                let opened = false;
                ws.onopen = (ev) => {
                    opened = true;
                    log("WS OPEN", url);
                    handlers.onopen && handlers.onopen(ev, ws, url);
                };
                ws.onmessage = (ev) => {
                    handlers.onmessage && handlers.onmessage(ev, ws, url);
                };
                ws.onerror = () => {
                    // 이유는 제공 안 됨 → close 로 이어짐
                };
                ws.onclose = (ev) => {
                    log("WS CLOSE", { url, code: ev.code, clean: ev.wasClean });
                    if (!opened) {
                        // 연결 자체가 안 됐으면 다음 후보 URL 시도
                        setTimeout(tryNext, 200);
                    } else {
                        handlers.onclose && handlers.onclose(ev, ws, url);
                    }
                };
            };

            tryNext();
            return () => {
                stopped = true;
                try {
                    ws && ws.close();
                } catch (_) { }
            };
        }

        // ─────────────────────────────────────────────────────────────
        // LiveChatClient (브라우저/사용자 측)
        // ─────────────────────────────────────────────────────────────
        const Client = {
            roomId: null,
            ws: null,
            stopConnector: null,
            lastHandoffAt: 0,
            autoReconnect: true,

            ensureRoomId() {
                if (!this.roomId) this.roomId = getRoomId();
                return this.roomId;
            },

            /**
             * ✅ 사용자 방 WebSocket 보장
             *  - /ws/chat/<roomId> 로 연결
             *  - 서버에서 오는 메시지를 QARAG에 표시
             */
            ensureRoomWS() {
                try {
                    const room = this.ensureRoomId();

                    // 이미 열린 소켓이 있으면 그대로 사용
                    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                        return this.ws;
                    }

                    // 이전 커넥터 정리
                    if (this.stopConnector) {
                        try {
                            this.stopConnector();
                        } catch (_) { }
                        this.stopConnector = null;
                    }

                    const urls = roomCandidates(room);
                    const self = this;

                    this.stopConnector = connectWithFallback(urls, {
                        onopen(_ev, sock, url) {
                            self.ws = sock;
                            log("ROOM_WS_OPEN", { url, room });
                            openPanel();
                        },
                        onmessage(ev, _sock, url) {
                            try {
                                const data = safeParse(ev.data);

                                // 외부 훅 먼저 호출
                                try {
                                    if (typeof window.onLiveChatMessage === "function") {
                                        window.onLiveChatMessage(data);
                                    }
                                } catch (_) { }

                                // QARAG 전용 후처리 (종료/버튼 상태 등)
                                const suppress = handleInboundForQarag(data);
                                if (suppress) return;

                                const sender = String(
                                    data.sender || data.role || data.type || "system"
                                ).toLowerCase();
                                const text = data.text || data.message || data.msg || "";
                                if (!text) return;

                                if (sender === "user") {
                                    // 같은 사용자 에코
                                    pushMsg("user", text);
                                } else if (sender === "operator") {
                                    // 상담사 메시지
                                    pushMsg("bot", text);
                                } else {
                                    // system / 기타
                                    pushMsg("bot", text);
                                }
                            } catch (e) {
                                log(
                                    "ROOM_WS_MSG_ERR",
                                    String(e && e.message ? e.message : e)
                                );
                            }
                        },
                        onclose(ev, sock, url) {
                            log("ROOM_WS_CLOSE", {
                                url,
                                code: ev.code,
                                clean: ev.wasClean,
                            });
                            if (self.ws === sock) {
                                self.ws = null;
                            }
                            if (self.autoReconnect && ev.code !== 1000) {
                                setTimeout(() => {
                                    if (!self.ws && self.autoReconnect) {
                                        self.ensureRoomWS();
                                    }
                                }, 1500);
                            }
                        },
                        onerror(err) {
                            log(
                                "ROOM_WS_ERR",
                                String(err && err.message ? err.message : err)
                            );
                        },
                    });

                    return null;
                } catch (e) {
                    log("ENSURE_ROOM_ERR", String(e && e.message ? e.message : e));
                    return null;
                }
            },

            /**
             * ✅ 운영자에게 한 번만 "상담 요청" 브로드캐스트
             *  - /ws/chat/master 로 접속해서 handoff 이벤트 1회 전송
             *  - extra 로 session_id 등 추가 정보 전송 가능
             */
            handoffOnce(extra) {
                const now = Date.now();
                if (now - this.lastHandoffAt < 1500) return; // 쿨다운(1.5초)

                this.lastHandoffAt = now;
                const room = this.ensureRoomId();
                const urlForOp = `${location.origin}/ragadmin/live-chat/?room=${encodeURIComponent(
                    room
                )}`;

                const payload = {
                    type: "handoff",
                    sender: "system",
                    room,
                    url: urlForOp,
                    text: "새 상담 요청이 도착했습니다.",
                    ts: now,
                    page: { title: document.title, path: location.pathname },
                };

                if (extra && typeof extra === "object") {
                    try {
                        Object.assign(payload, extra);
                    } catch (_) { }
                }

                const urls = lobbyCandidates();

                connectWithFallback(urls, {
                    onopen(_ev, sock, url) {
                        try {
                            sock.send(JSON.stringify(payload));
                            log("WS LOBBY SEND", { url, payload });
                        } catch (e) {
                            log(
                                "ERR LOBBY SEND",
                                String(e && e.message ? e.message : e)
                            );
                        } finally {
                            // 짧게 사용 후 정리
                            setTimeout(() => {
                                try {
                                    sock.close();
                                } catch (_) { }
                            }, 200);
                        }
                    },
                    onmessage() { },
                    onclose() { },
                    onerror(err) {
                        log(
                            "LOBBY_WS_ERR",
                            String(err && err.message ? err.message : err)
                        );
                    },
                });
            },

            /**
             * ✅ 사용자 → 운영자 메시지 전송
             *  - 실제 WebSocket 으로 서버에 JSON 전송
             *  - 메시지 그리기는 서버 에코를 받아서 onmessage 에서 처리
             */
            sendToOperator(text) {
                try {
                    const msg = String(text || "").trim();
                    if (!msg) return;

                    const sock = this.ws;
                    if (!sock || sock.readyState !== WebSocket.OPEN) {
                        // 아직 연결 전이면 우선 연결 시도만 하고 안내 메시지
                        this.ensureRoomWS();
                        pushMsg(
                            "bot",
                            "아직 상담사 연결이 준비 중입니다. 잠시 후 다시 시도해 주세요."
                        );
                        log("ROOM_WS_NOT_READY", {
                            state: sock && sock.readyState,
                        });
                        return;
                    }

                    const payload = {
                        sender: "user",
                        text: msg,
                        ts: Date.now(),
                    };

                    sock.send(JSON.stringify(payload));
                    // 실제 말풍선은 서버 에코를 받아 onmessage 에서 pushMsg('user', ...) 수행
                } catch (e) {
                    log("ERR WS SAY", String(e && e.message ? e.message : e));
                }
            },

            /**
             * ✅ 사용자 쪽에서 “상담 종료” 버튼을 눌렀을 때 서버/상담사에 알리기
             *  - 현재 방 WebSocket을 통해 type: "end" 전송
             *  - RoomConsumer가 LiveChatSession을 종료 상태로 만들고
             *    실시간 상담 콘솔에도 종료 이벤트가 브로드캐스트됨
             */
            endFromUser(reasonText) {
                try {
                    const sock = this.ws;
                    const now = Date.now();
                    const payload = {
                        sender: "user",
                        type: "end",
                        text:
                            reasonText ||
                            "[사용자]가 상담 종료 버튼을 눌렀습니다.",
                        ts: now,
                    };

                    if (sock && sock.readyState === WebSocket.OPEN) {
                        sock.send(JSON.stringify(payload));
                        log("USER_END_SEND", payload);
                    } else {
                        // 소켓이 이미 끊겨 있으면 서버에 알리지는 못하지만,
                        // 최소한 재연결은 시도하지 않도록 플래그만 내려둔다.
                        log("USER_END_NO_SOCKET", {
                            state: sock && sock.readyState,
                        });
                    }

                    // 사용자가 종료한 이후에는 자동 재연결은 하지 않음
                    this.autoReconnect = false;
                } catch (e) {
                    log(
                        "USER_END_ERR",
                        String(e && e.message ? e.message : e)
                    );
                }
            },
        };

        // ─────────────────────────────────────────────────────────────
        // HTTP 헬퍼 (CSRF 쿠키 / 상담 가능 여부 / 세션 생성)
        // ─────────────────────────────────────────────────────────────
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

        // ✅ 상담사 콘솔 연결 여부 체크
        async function checkLivechatAvailability() {
            try {
                const res = await fetch("/api/livechat/availability/", {
                    method: "GET",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });

                if (!res.ok) {
                    pushMsg(
                        "bot",
                        "지금은 상담 준비중입니다. 나중에 다시 시도해주시길 바랍니다."
                    );
                    return false;
                }

                const data = await res.json().catch(() => null);
                if (!data || !data.ok || !data.available) {
                    pushMsg(
                        "bot",
                        "지금은 상담 준비중입니다. 나중에 다시 시도해주시길 바랍니다."
                    );
                    return false;
                }
                return true;
            } catch (e) {
                log("LIVECHAT_AVAIL_ERR", e && e.message ? e.message : e);
                pushMsg(
                    "bot",
                    "지금은 상담 준비중입니다. 나중에 다시 시도해주시길 바랍니다."
                );
                return false;
            }
        }

        /**
         * 실시간 상담 세션 요청
         * - 백엔드 구현에 따라
         *   1) /api/livechat/request  (새 버전)
         *   2) /livechat/api/request/ (예전 버전)
         *   을 순서대로 시도
         *
         * 반환값:
         *   { sessionId, code, room } 또는 null
         */
        async function requestLiveChatSession() {
            const endpoints = ["/api/livechat/request", "/livechat/api/request/"];

            const roomIdForServer = Client.ensureRoomId();
            const payload = {
                from: "qarag",
                room: roomIdForServer,
                page: { title: document.title, path: location.pathname },
            };

            const headers = {
                "Content-Type": "application/json",
            };
            const csrftoken = getCookie("csrftoken");
            if (csrftoken) headers["X-CSRFToken"] = csrftoken;

            let lastError = null;

            for (const url of endpoints) {
                try {
                    const resp = await fetch(url, {
                        method: "POST",
                        headers,
                        body: JSON.stringify(payload),
                    });

                    if (!resp.ok) {
                        lastError = "HTTP " + resp.status;
                        continue;
                    }

                    const data = await resp.json().catch(() => null);

                    if (data && data.ok === false) {
                        lastError = data.error || data.message || "응답 ok=false";
                        continue;
                    }

                    const sessionId = data && typeof data.session_id !== "undefined"
                        ? data.session_id
                        : null;
                    const code =
                        (data && (data.code || data.session_id)) ||
                        null;

                    if (!sessionId && !code) {
                        lastError = "no session id/code";
                        continue;
                    }

                    // 👉 QARAG 창에 대기 코드 안내 메세지 하나 찍어주기
                    pushMsg(
                        "bot",
                        "상담사 연결을 요청했습니다.\n\n곧 상담사가 입장하면 안내 메시지가 표시됩니다."
                    );

                    return {
                        sessionId: sessionId,
                        code: code ? String(code) : null,
                        room: roomIdForServer,
                    };
                } catch (err) {
                    lastError = err && err.message ? err.message : String(err);
                    log("LIVECHAT_REQ_ERR", { url, err: lastError });
                    continue;
                }
            }

            console.error("livechat request failed:", lastError);
            alert("상담사 연결 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.");
            return null;
        }

        // ─────────────────────────────────────────────────────────────
        // QARAG UI: 상담 연결 / 동의 모달 / 상담 종료 버튼
        // ─────────────────────────────────────────────────────────────
        function initQaragLivechatUI() {
            btnConnectLive = $("#btnConnectLive");
            btnEndLive = $("#btnEndLive");
            overlay = $("#livechatOverlay");
            agreeBtn = $("#livechatAgreeBtn");
            cancelBtn = $("#livechatCancelBtn");
            msgBox = $("#qaragMessages");
            inputBox = $("#qaragInput");
            sendBtn = $("#qaragSendBtn");

            // QARAG 패널 없으면 스킵
            if (!btnConnectLive || !msgBox) {
                return;
            }

            const st = (g.__qaragState = g.__qaragState || {});
            if (typeof st.liveEnded === "undefined") st.liveEnded = false;
            if (typeof st.liveSessionCode === "undefined") st.liveSessionCode = null;
            if (typeof st.liveSessionId === "undefined") st.liveSessionId = null;

            // 처음 상태: 상담 종료 버튼 숨김
            if (btnConnectLive) {
                btnConnectLive.disabled = false;
            }
            hideEndButton();

            // 상담사 연결 버튼 → 동의 모달
            btnConnectLive.addEventListener("click", function () {
                if (st.liveEnded) {
                    pushMsg(
                        "bot",
                        "이미 상담이 종료되었습니다. 새 상담을 원하시면 페이지를 새로고침해 주세요."
                    );
                    return;
                }

                // 이미 세션 코드가 있다면 (대기/진행 중) 중복 요청 안 함
                if (st.liveSessionCode) {
                    return;
                }

                if (overlay) {
                    overlay.removeAttribute("hidden");
                } else {
                    const ok = window.confirm(
                        "욕설·폭언·성희롱 등은 상담 중단 및 서비스 제한 사유가 될 수 있습니다.\n\n위 안내를 읽고 동의하시면 [확인]을 눌러 주세요."
                    );
                    if (ok) {
                        handleAgreeFlow();
                    }
                }
            });

            // 모달 취소 버튼
            if (cancelBtn && overlay) {
                cancelBtn.addEventListener("click", function () {
                    overlay.setAttribute("hidden", "true");
                });
            }

            // 모달 동의 버튼 → 실제 상담 요청
            if (agreeBtn) {
                agreeBtn.addEventListener("click", function () {
                    handleAgreeFlow();
                });
            }

            // 상담 종료 버튼 (질문 챗봇 측)
            if (btnEndLive) {
                btnEndLive.addEventListener("click", function () {
                    if (st.liveEnded) {
                        pushMsg(
                            "bot",
                            "이미 상담이 종료되었습니다. 새 상담을 원하시면 페이지를 새로고침해 주세요."
                        );
                        return;
                    }

                    if (!window.confirm("상담을 종료하시겠습니까?")) {
                        return;
                    }

                    // ✅ 서버/실시간 상담 콘솔에도 종료 알림 전파
                    try {
                        if (
                            window.LiveChatClient &&
                            typeof window.LiveChatClient.endFromUser === "function"
                        ) {
                            window.LiveChatClient.endFromUser();
                        }
                    } catch (_) { }

                    // ✅ 질문 챗봇 UI 종료 처리
                    setEndedUI();
                });
            }

            // 동의 후 실제 상담 요청 처리
            async function handleAgreeFlow() {
                if (overlay) {
                    overlay.setAttribute("hidden", "true");
                }

                // 1) 상담 가능 여부 확인
                const okAvail = await checkLivechatAvailability();
                if (!okAvail) {
                    if (btnConnectLive) {
                        btnConnectLive.disabled = false;
                    }
                    return;
                }

                // 2) 세션 생성 요청
                const result = await requestLiveChatSession();
                if (!result) {
                    if (btnConnectLive) {
                        btnConnectLive.disabled = false;
                    }
                    return;
                }

                const { sessionId, code, room } = result;

                const st = (g.__qaragState = g.__qaragState || {});
                st.liveSessionCode = code || sessionId;
                st.liveSessionId = sessionId || null;
                st.liveEnded = false;

                if (btnConnectLive) {
                    btnConnectLive.disabled = true; // 중복 요청 방지
                }

                // 3) 운영자 콘솔 로비에 handoff 브로드캐스트 (session_id 포함)
                try {
                    Client.handoffOnce({
                        session_id: sessionId || null,
                        room: room || Client.ensureRoomId(),
                    });
                } catch (_) { }

                // 4) 사용자 방 WebSocket 연결 보장
                try {
                    Client.ensureRoomWS();
                } catch (_) { }
            }
        }

        // ─────────────────────────────────────────────────────────────
        // QARAG → 상담사 메시지 전송 헬퍼 (다른 스크립트에서 재사용)
        // ─────────────────────────────────────────────────────────────
        function sendLiveChatIfAvailable(text) {
            try {
                const msg = String(text || "").trim();
                if (!msg) return;
                if (
                    window.LiveChatClient &&
                    typeof window.LiveChatClient.sendToOperator === "function"
                ) {
                    window.LiveChatClient.sendToOperator(msg);
                }
            } catch (_) { }
        }

        // 다른 스크립트(예: QARAG 위젯 JS)에서 직접 부를 수 있게 전역 헬퍼도 노출
        if (typeof window !== "undefined") {
            window.sendLiveChatText =
                window.sendLiveChatText || sendLiveChatIfAvailable;
        }

        // ─────────────────────────────────────────────────────────────
        // 상담 종료 후 질문 챗봇에서 추가 입력/전송 막기 (sendQarag 가드)
        // ─────────────────────────────────────────────────────────────
        function setupSendQaragGuard() {
            const origSend = g.sendQarag;
            if (typeof origSend !== "function") {
                return;
            }

            g.sendQarag = function (ev) {
                try {
                    const st = g.__qaragState || {};
                    if (st.liveEnded) {
                        if (ev && typeof ev.preventDefault === "function") {
                            ev.preventDefault();
                        }
                        pushMsg(
                            "bot",
                            "이미 상담이 종료된 상태입니다. 새 상담을 원하시면 페이지를 새로고침해 주세요."
                        );
                        return false;
                    }
                } catch (_) { }

                return origSend(ev);
            };
        }

        // ─────────────────────────────────────────────────────────────
        // 전역 브리지
        // ─────────────────────────────────────────────────────────────

        // 사용자 방 WebSocket 보장
        window.ensureUserWS = function () {
            return Client.ensureRoomWS();
        };

        // 운영자에게 "새 상담 요청" 알림 한 번 보내기 (하위 호환)
        window.sendHandoffOnce = function () {
            return Client.handoffOnce();
        };

        // 예전 카카오/페이지 이동용 헬퍼 호환용(지금은 안 써도 됨)
        window.openCounselorPage = function () {
            const room = Client.ensureRoomId();
            const p = new URLSearchParams();
            p.set("room", room);
            const url = "/assistant/?" + p.toString();
            window.open(url, "_blank", "noopener,noreferrer");
        };
        window.openCounselor = window.openCounselorPage;

        // QARAG 등에서 직접 접근할 수 있는 클라이언트 객체
        window.LiveChatClient = Client;

        // QARAG 쪽에서 “상담사 연결” 플로우를 한 번에 쓰고 싶으면:
        //   if (window.startLiveChatFromQarag) window.startLiveChatFromQarag();
        window.startLiveChatFromQarag = function () {
            try {
                Client.ensureRoomWS();
                Client.handoffOnce();
            } catch (e) {
                log(
                    "START_LIVECHAT_ERR",
                    String(e && e.message ? e.message : e)
                );
            }
        };

        // 원하면 직접 호출해서 종료 신호만 날리는 브리지도 제공
        window.endLiveChatFromQarag = function (reasonText) {
            try {
                Client.endFromUser(reasonText);
            } catch (e) {
                log(
                    "END_LIVECHAT_BRIDGE_ERR",
                    String(e && e.message ? e.message : e)
                );
            }
        };

        // ─────────────────────────────────────────────────────────────
        // 초기화
        // ─────────────────────────────────────────────────────────────
        document.addEventListener("DOMContentLoaded", () => {
            try {
                log("INIT", { room: Client.ensureRoomId() });
            } catch (e) {
                log("ERR INIT", String(e && e.message ? e.message : e));
            }

            try {
                initQaragLivechatUI();
            } catch (e) {
                log(
                    "ERR_UI_INIT",
                    String(e && e.message ? e.message : e)
                );
            }

            try {
                setupSendQaragGuard();
            } catch (e) {
                log(
                    "ERR_SEND_GUARD",
                    String(e && e.message ? e.message : e)
                );
            }
        });
    } catch (e) {
        try {
            if (typeof window.dglog === "function") {
                window.dglog("LIVECHAT_FATAL", e);
            } else {
                console.error("[livechat fatal]", e);
            }
        } catch (_) { }
    }
})();
