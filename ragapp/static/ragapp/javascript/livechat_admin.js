/* ragapp/static/ragapp/javascript/livechat_admin.js */
(function () {
    'use strict';

    try {
        // ─────────────────────────────────────────────────────────────
        // 경량 로거
        // ─────────────────────────────────────────────────────────────
        const log = (tag, data) => {
            try {
                if (typeof window.dglog === 'function') {
                    window.dglog('LIVECHAT_ADMIN:' + tag, data);
                } else {
                    const ts = new Date().toISOString().slice(11, 23);
                    console.log('[livechat_admin ' + ts + '] ' + tag, data ?? '');
                }
            } catch (_) { }
        };

        const $ = (sel, root = document) => root.querySelector(sel);
        const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

        const scheme = () => (location.protocol === 'https:' ? 'wss' : 'ws');
        const mkWS = (path) => `${scheme()}://${location.host}${path}`;

        // CSRF 쿠키 헬퍼
        function getCookie(name) {
            try {
                const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
                return m ? decodeURIComponent(m.pop()) : '';
            } catch (_) {
                return '';
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 상태
        // ─────────────────────────────────────────────────────────────
        const state = {
            lobbySocket: null,
            lobbyConnected: false,
            lobbyRetryCount: 0,
            pendingRooms: {},      // roomId → { room, url, ts, page, text, code, ... }

            roomSocket: null,
            currentRoom: null,
            greetedRooms: {},      // 인삿말을 한번 보낸 방 기록
            setEndButtonEnabled: null, // 상담 종료 버튼 on/off 헬퍼

            doneCount: 0,          // 오늘 이 콘솔에서 종료한 세션 수(프론트 기준)
            roomEnded: false,      // 현재 방이 종료된 상태인지 여부 (로컬/원격 상관없이)
            sessionSaved: false    // 현재 방 종료 후 상담 기록 저장 여부
        };

        let notifyAudio = null;
        let sessionSaveBtn = null;
        let sessionStatusPill = null;

        // ─────────────────────────────────────────────────────────────
        // 공통: 알림 사운드
        // ─────────────────────────────────────────────────────────────
        function playNotify(kind) {
            try {
                if (!notifyAudio) {
                    notifyAudio = $('#livechatNotifySound');
                }
                if (!notifyAudio) return;

                notifyAudio.currentTime = 0;
                const p = notifyAudio.play();
                if (p && typeof p.catch === 'function') {
                    p.catch(() => { /* 브라우저 자동재생 차단 시 무시 */ });
                }
            } catch (e) {
                log('NOTIFY_SOUND_ERR', e && e.message ? e.message : e);
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 유틸: 시간 포맷
        // ─────────────────────────────────────────────────────────────
        function formatTime(ts) {
            if (!ts) return '';
            try {
                const d = new Date(ts);
                if (Number.isNaN(d.getTime())) return '';
                return d.toLocaleTimeString('ko-KR', {
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                });
            } catch (_) {
                return '';
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 상단 요약 바 업데이트
        // ─────────────────────────────────────────────────────────────
        function updateSummaryUI() {
            try {
                const waitCount = Object.keys(state.pendingRooms || {}).length;
                const activeCount =
                    state.roomSocket &&
                        state.currentRoom &&
                        state.roomSocket.readyState === WebSocket.OPEN
                        ? 1
                        : 0;
                const doneCount = state.doneCount || 0;

                const elWait = $('#summaryWaitCount');
                const elActive = $('#summaryActiveCount');
                const elDone = $('#summaryDoneCount');
                const elLobbyCount = $('#livechatLobbyCount');

                if (elLobbyCount) elLobbyCount.textContent = String(waitCount);
                if (elWait) elWait.textContent = String(waitCount);
                if (elActive) elActive.textContent = String(activeCount);
                if (elDone) elDone.textContent = String(doneCount);
            } catch (_) { }
        }

        // ─────────────────────────────────────────────────────────────
        // 방 헤더 메타(페이지 / 시작 / 코드) 세팅
        // ─────────────────────────────────────────────────────────────
        function setRoomMetaFromRequest(roomId, req) {
            try {
                const pageEl = $('#livechatRoomPage');
                const startedEl = $('#livechatRoomStartedAt');
                const codeEl = $('#livechatRoomCode');

                if (!pageEl && !startedEl && !codeEl) return;

                const pageTitle =
                    req && req.page && req.page.title ? req.page.title : '(제목 없음)';
                const pagePath =
                    req && req.page && req.page.path ? req.page.path : '';
                const ts = (req && req.ts) || Date.now();
                const code =
                    (req && (req.session_id || req.code)) || roomId || '-';

                if (pageEl) {
                    pageEl.textContent = pagePath
                        ? pageTitle + ' · ' + pagePath
                        : pageTitle;
                }
                if (startedEl) {
                    startedEl.textContent = formatTime(ts);
                }
                if (codeEl) {
                    codeEl.textContent = code;
                }
            } catch (e) {
                log('SET_ROOM_META_ERR', e && e.message ? e.message : e);
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 상담 기록 저장 상태 표시(하단 뱃지)
        // ─────────────────────────────────────────────────────────────
        function setSessionStatus(mode) {
            try {
                // ✅ id 없으면 class 로라도 잡도록 보강
                if (!sessionStatusPill) {
                    sessionStatusPill =
                        document.querySelector('#sessionStatusPill') ||
                        document.querySelector('.session-status-pill');
                }
                if (!sessionStatusPill) return;

                // 기본 클래스 초기화
                sessionStatusPill.className = 'session-status-pill';

                let text = '';

                switch (mode) {
                    case 'idle':
                        sessionStatusPill.classList.add('session-status-pill--idle');
                        text = '진행 중 · 저장 준비';
                        break;
                    case 'need':
                        sessionStatusPill.classList.add('session-status-pill--need');
                        text = '종료됨 · 저장 필요';
                        break;
                    case 'saving':
                        sessionStatusPill.classList.add('session-status-pill--saving');
                        text = '상담 기록 저장 중...';
                        break;
                    case 'ok':
                        sessionStatusPill.classList.add('session-status-pill--ok');
                        text = '저장 완료 · 다음 상담 가능';
                        break;
                    case 'error':
                        sessionStatusPill.classList.add('session-status-pill--error');
                        text = '저장 실패 · 다시 시도해 주세요';
                        break;
                    default:
                        sessionStatusPill.classList.add('session-status-pill--idle');
                        text = '진행 중 · 저장 준비';
                        break;
                }

                sessionStatusPill.textContent = text;
            } catch (_) { }
        }


        // 종료된 세션: 저장 필요 상태로 표시
        function markSessionNeedSave() {
            state.roomEnded = true;
            if (!state.sessionSaved) {
                // 아직 저장을 안 한 경우에만 "저장 필요"로 표시
                setSessionStatus('need');
            }

            if (!sessionSaveBtn) {
                sessionSaveBtn = $('#sessionSaveBtn');
            }
            if (sessionSaveBtn && !state.sessionSaved) {
                sessionSaveBtn.disabled = false;
            }
        }

        // 새 방 들어갈 때 상태 초기화
        function resetSessionStatus() {
            state.roomEnded = false;
            state.sessionSaved = false;

            if (!sessionSaveBtn) {
                sessionSaveBtn = $('#sessionSaveBtn');
            }
            if (sessionSaveBtn) {
                sessionSaveBtn.disabled = true;
            }
            setSessionStatus('idle');
        }

        // ─────────────────────────────────────────────────────────────
        // 최근 상담 세션 리스트 실시간 갱신
        //  - 템플릿의 .session-list 요소에 data-recent-url 이 있으면 사용
        //  - JSON { ok:true, html:"<li>...</li>..." } 또는 HTML 그대로 모두 지원
        // ─────────────────────────────────────────────────────────────
        async function refreshRecentSessions(options) {
            const opts = options || {};
            try {
                const list = $('.session-list');
                if (!list) return;

                const url = list.dataset.recentUrl;
                if (!url) return;

                const res = await fetch(url, {
                    method: 'GET',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });

                if (!res.ok) {
                    if (!opts.silent) {
                        log('RECENT_SESS_HTTP_ERR', res.status);
                    }
                    return;
                }

                const ct = (res.headers.get('Content-Type') || '').toLowerCase();
                let html = '';

                if (ct.includes('application/json')) {
                    const data = await res.json().catch(() => null);
                    if (!data || data.ok === false) {
                        if (!opts.silent) {
                            log('RECENT_SESS_JSON_ERR', data);
                        }
                        return;
                    }
                    html = data.html || data.items_html || '';
                } else {
                    html = await res.text().catch(() => '');
                }

                if (!html) return;
                list.innerHTML = html;
            } catch (e) {
                if (!opts.silent) {
                    log('RECENT_SESS_ERR', e && e.message ? e.message : e);
                }
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 로비(대기 요청) UI 렌더
        // ─────────────────────────────────────────────────────────────
        function renderLobbyList() {
            const tbody = $('#livechatLobbyBody');
            const empty = $('#livechatLobbyEmpty');
            if (!tbody) return;

            tbody.innerHTML = '';

            const rooms = Object.values(state.pendingRooms)
                .sort((a, b) => (a.ts || 0) - (b.ts || 0));

            if (!rooms.length) {
                if (empty) empty.style.display = 'block';
                updateSummaryUI();
                return;
            }
            if (empty) empty.style.display = 'none';

            for (const req of rooms) {
                const roomId = String(req.room || '');
                if (!roomId) continue;

                const tr = document.createElement('tr');
                tr.dataset.room = roomId;

                // 방 ID
                const tdRoom = document.createElement('td');
                const roomSpan = document.createElement('span');
                roomSpan.className = 'lc-lobby-room';
                roomSpan.textContent = roomId;
                tdRoom.appendChild(roomSpan);

                // 페이지 정보
                const tdPage = document.createElement('td');
                const pageDiv = document.createElement('div');
                pageDiv.className = 'lc-lobby-page';
                const pageTitle = req.page && req.page.title ? req.page.title : '(제목 없음)';
                const pagePath = req.page && req.page.path ? req.page.path : '';
                pageDiv.textContent = pageTitle + (pagePath ? ' · ' + pagePath : '');
                tdPage.appendChild(pageDiv);

                // 시간
                const tdTime = document.createElement('td');
                tdTime.className = 'lc-lobby-time';
                tdTime.textContent = formatTime(req.ts || Date.now());

                // 동작
                const tdActions = document.createElement('td');
                tdActions.style.textAlign = 'right';
                const actionWrap = document.createElement('div');
                actionWrap.className = 'lc-lobby-actions';

                const btnJoin = document.createElement('button');
                btnJoin.type = 'button';
                btnJoin.className = 'lc-btn lc-btn-primary lc-btn-xs';
                btnJoin.textContent = '연결';
                btnJoin.setAttribute('data-join-room', roomId);

                actionWrap.appendChild(btnJoin);

                if (req.url) {
                    const aLink = document.createElement('a');
                    aLink.href = req.url;
                    aLink.target = '_blank';
                    aLink.rel = 'noopener noreferrer';
                    aLink.className = 'lc-btn lc-btn-soft lc-btn-xs';
                    aLink.textContent = '새 창';
                    actionWrap.appendChild(aLink);
                }

                tdActions.appendChild(actionWrap);

                tr.appendChild(tdRoom);
                tr.appendChild(tdPage);
                tr.appendChild(tdTime);
                tr.appendChild(tdActions);

                tbody.appendChild(tr);
            }

            updateSummaryUI();
        }

        // ─────────────────────────────────────────────────────────────
        // 로비 상태 표시
        // ─────────────────────────────────────────────────────────────
        function setLobbyStatus(text, strong) {
            const el = $('#livechatLobbyStatus');
            if (!el) return;
            el.innerHTML = '로비 연결 상태: <strong>' + (strong || text || '') + '</strong>';
        }

        // ─────────────────────────────────────────────────────────────
        // 방 상태 표시 / 방 ID 표시
        // ─────────────────────────────────────────────────────────────
        function setRoomStatus(connected) {
            const el = $('#livechatRoomStatus');
            if (!el) return;
            if (connected) {
                el.innerHTML = '<span class="lc-dot-online"></span> 연결됨';
            } else {
                el.innerHTML = '<span class="lc-dot-offline"></span> 연결되지 않음';
            }
        }

        function setRoomIdLabel(roomId) {
            const el = $('#livechatRoomIdLabel');
            if (!el) return;
            if (!roomId) {
                el.textContent = '방을 선택해 주세요';
            } else {
                el.textContent = roomId;
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 방 메시지 UI
        // ─────────────────────────────────────────────────────────────
        function clearRoomMessages() {
            const box = $('#livechatRoomMessages');
            const empty = $('#livechatRoomEmpty');
            if (box) {
                box.innerHTML = '';
            }
            if (empty) {
                empty.style.display = 'block';
            }
        }

        function appendRoomMessage(sender, text, ts) {
            const box = $('#livechatRoomMessages');
            if (!box) return;

            const empty = $('#livechatRoomEmpty');
            if (empty) {
                empty.style.display = 'none';
            }

            const s = (sender || 'system').toLowerCase();
            const row = document.createElement('div');
            row.className = 'lc-msg-row ' + (s === 'operator' ? 'operator' : (s === 'user' ? 'user' : 'system'));

            const bubble = document.createElement('div');
            bubble.className = 'lc-msg-bubble ' + (s === 'operator' ? 'operator' :
                s === 'user' ? 'user' : 'system');
            bubble.textContent = String(text || '');

            row.appendChild(bubble);
            box.appendChild(row);

            if (s !== 'system') {
                const meta = document.createElement('div');
                meta.className = 'lc-msg-meta ' + (s === 'operator' ? 'operator' : 'user');
                const roleLabel = (s === 'operator' ? '상담사' : '사용자');
                const timeLabel = formatTime(ts || Date.now());
                meta.textContent = roleLabel + ' · ' + timeLabel;
                box.appendChild(meta);
            }

            box.scrollTop = box.scrollHeight;
        }

        // ─────────────────────────────────────────────────────────────
        // WebSocket: 로비 연결 (/ws/chat/master)
        // ─────────────────────────────────────────────────────────────
        function connectLobby() {
            try {
                if (state.lobbySocket) {
                    try { state.lobbySocket.close(); } catch (_) { }
                    state.lobbySocket = null;
                }

                const url = mkWS('/ws/chat/master');
                log('LOBBY_CONNECT → ' + url);

                const ws = new WebSocket(url);
                state.lobbySocket = ws;

                ws.onopen = function () {
                    state.lobbyConnected = true;
                    state.lobbyRetryCount = 0;
                    log('LOBBY_OPEN', url);
                    setLobbyStatus('연결됨', '연결됨');
                    updateSummaryUI();
                };

                ws.onmessage = function (ev) {
                    let data = null;
                    try {
                        data = JSON.parse(ev.data);
                    } catch (e) {
                        log('LOBBY_MSG_PARSE_ERR', String(e));
                        return;
                    }
                    if (!data || typeof data !== 'object') return;

                    const t = String(data.type || '').toLowerCase();
                    log('LOBBY_EVENT', data);

                    // 🔹 상담 기록 저장 브로드캐스트 → 최근 세션 리스트 즉시 새로고침
                    if (t === 'session_saved') {
                        try {
                            refreshRecentSessions({ silent: true });
                        } catch (e) {
                            log('RECENT_SESS_WS_ERR', e && e.message ? e.message : e);
                        }
                        return;
                    }

                    // 새 상담 요청 (handoff)
                    if (t === 'handoff') {
                        const roomId = String(data.room || '');
                        if (!roomId) return;
                        state.pendingRooms[roomId] = data;
                        renderLobbyList();
                        playNotify('handoff');
                        return;
                    }

                    // 상담 종료 알림 등
                    if (t === 'closed' || t === 'release' || t === 'end') {
                        const roomId = String(data.room || '');
                        if (roomId && state.pendingRooms[roomId]) {
                            delete state.pendingRooms[roomId];
                            renderLobbyList();
                        }
                        return;
                    }
                };

                ws.onclose = function () {
                    state.lobbyConnected = false;
                    log('LOBBY_CLOSE', url);
                    setLobbyStatus('연결 끊김(자동 재시도 중)', '연결 끊김(자동 재시도 중)');
                    updateSummaryUI();

                    // 재시도 (간단한 backoff)
                    const delay = Math.min(10000, 2000 + state.lobbyRetryCount * 1000);
                    state.lobbyRetryCount += 1;
                    setTimeout(() => {
                        connectLobby();
                    }, delay);
                };

                ws.onerror = function () {
                    // onclose에서 재시도 처리
                };
            } catch (e) {
                log('LOBBY_CONNECT_FATAL', String(e));
                setLobbyStatus('연결 오류', '연결 오류');
            }
        }

        // ─────────────────────────────────────────────────────────────
        // 상담 종료 (운영자 측에서 종료)
        // ─────────────────────────────────────────────────────────────
        function endCurrentRoom() {
            if (!state.currentRoom || !state.roomSocket) return;
            const sock = state.roomSocket;
            if (sock.readyState !== WebSocket.OPEN) {
                alert('상담 방 WebSocket이 아직 연결되지 않았습니다.');
                return;
            }

            const payload = {
                sender: 'operator',
                type: 'end',
                text: '상담사가 상담을 종료했습니다.',
                ts: Date.now()
            };

            // 세션 메타(문의 유형 / 메모 / 상세 기록) 같이 보내기
            try {
                const typeEl = $('#sessionType');
                const noteEl = $('#sessionNote');
                const detailEl = $('#sessionDetail');
                const sessionType = typeEl ? String(typeEl.value || '') : '';
                const sessionNote = noteEl ? String(noteEl.value || '') : '';
                const sessionDetail = detailEl ? String(detailEl.value || '') : '';

                // "상담기록 필수" 강제
                if (!sessionType && !sessionNote && !sessionDetail) {
                    alert('상담 유형 또는 요약/상세 메모를 하나 이상 입력해야 상담을 종료할 수 있습니다.');
                    return;
                }

                if (sessionType) payload.session_type = sessionType;
                if (sessionNote) payload.session_note = sessionNote;
                if (sessionDetail) payload.session_detail = sessionDetail;

                // 종료 후 필드 비우지 않고 그대로 둠(저장 버튼에서 재사용)
                if (typeEl) typeEl.value = sessionType;
                if (noteEl) noteEl.value = sessionNote;
                if (detailEl) detailEl.value = sessionDetail;
            } catch (e) {
                log('SESSION_META_READ_ERR', e && e.message ? e.message : e);
            }

            // 이 시점부터는 이 방을 종료된 상태로 취급
            state.roomEnded = true;
            state.sessionSaved = false;

            try {
                sock.send(JSON.stringify(payload));
                log('ROOM_END_SEND', payload);
            } catch (e) {
                log('ROOM_END_ERR', String(e));
            }

            try {
                sock.close(1000, 'operator end');
            } catch (_) { }

            appendRoomMessage('system', '상담을 종료했습니다.', Date.now());
            state.doneCount = (state.doneCount || 0) + 1;
            setRoomStatus(false);
            if (typeof state.setEndButtonEnabled === 'function') {
                state.setEndButtonEnabled(false);
            }
            markSessionNeedSave();
            updateSummaryUI();
        }

        // ─────────────────────────────────────────────────────────────
        // WebSocket: 방 접속 (/ws/chat/<room>)
        // ─────────────────────────────────────────────────────────────
        function connectRoom(roomId) {
            if (!roomId) return;

            // 종료된 상담이 있는데 저장 안 했으면 새 방 연결 막기
            if (state.currentRoom && state.roomEnded && !state.sessionSaved) {
                alert('이전 상담의 기록을 먼저 저장해 주세요.');
                return;
            }

            try {
                if (state.roomSocket) {
                    try { state.roomSocket.close(); } catch (_) { }
                    state.roomSocket = null;
                }
            } catch (_) { }

            state.currentRoom = roomId;
            resetSessionStatus();

            setRoomIdLabel(roomId);
            setRoomStatus(false);
            clearRoomMessages();
            if (typeof state.setEndButtonEnabled === 'function') {
                state.setEndButtonEnabled(false);
            }
            updateSummaryUI();

            const url = mkWS('/ws/chat/' + encodeURIComponent(roomId));
            log('ROOM_CONNECT → ' + url);

            let ws = null;
            try {
                ws = new WebSocket(url);
            } catch (e) {
                log('ROOM_WS_NEW_ERR', String(e));
                appendRoomMessage('system', '방 WebSocket 연결에 실패했습니다. 콘솔 로그를 확인해 주세요.', Date.now());
                return;
            }

            state.roomSocket = ws;

            ws.onopen = function () {
                log('ROOM_OPEN', url);
                setRoomStatus(true);
                if (typeof state.setEndButtonEnabled === 'function') {
                    state.setEndButtonEnabled(true);
                }
                updateSummaryUI();

                // 이 방에 처음 연결될 때만 자동 인삿말 전송
                try {
                    if (!state.greetedRooms[roomId]) {
                        const greeting = '안녕하세요 김동건의 포트폴리오 입니다. 무엇을 도와드릴까요?';
                        const payload = {
                            sender: 'operator',
                            text: greeting,
                            ts: Date.now()
                        };
                        ws.send(JSON.stringify(payload));
                        state.greetedRooms[roomId] = true;
                        log('ROOM_GREETING_SENT', { roomId, greeting });
                    }
                } catch (e) {
                    log('ROOM_GREETING_ERR', e && e.message ? e.message : e);
                }
            };

            ws.onmessage = function (ev) {
                let data = null;
                try {
                    data = JSON.parse(ev.data);
                } catch (e) {
                    log('ROOM_MSG_PARSE_ERR', String(e));
                    return;
                }
                if (!data || typeof data !== 'object') return;

                const t = String(data.type || '').toLowerCase();
                const sender = data.sender || 'system';
                const text = data.text || '';
                const ts = data.ts || Date.now();
                const txt = String(text || '');

                // 사용자 메시지 도착 시 알림음
                try {
                    if (String(sender).toLowerCase() === 'user') {
                        playNotify('user');
                    }
                } catch (_) { }

                // 종료로 간주할 패턴들
                const isEndLike =
                    t === 'end' ||
                    t === 'closed' ||
                    txt.indexOf('상담을 종료했습니다') !== -1 ||
                    txt.indexOf('상담이 종료되었습니다') !== -1;

                if (isEndLike) {
                    const msg = txt || (String(sender).toLowerCase() === 'user'
                        ? '사용자가 상담을 종료했습니다.'
                        : '상담이 종료되었습니다.');
                    appendRoomMessage('system', msg, ts);
                    state.doneCount = (state.doneCount || 0) + 1;
                    state.roomEnded = true;
                    if (!state.sessionSaved) {
                        markSessionNeedSave();
                    }
                    try { ws.close(1000, 'client end'); } catch (_) { }
                    if (typeof state.setEndButtonEnabled === 'function') {
                        state.setEndButtonEnabled(false);
                    }
                    setRoomStatus(false);
                    updateSummaryUI();
                    return;
                }

                appendRoomMessage(sender, text, ts);
            };

            ws.onclose = function (ev) {
                log('ROOM_CLOSE', { url, code: ev.code, clean: ev.wasClean });

                // 여기까지 roomEnded가 false였다면,
                // "사용자 종료/네트워크 종료"로 간주
                if (!state.roomEnded) {
                    appendRoomMessage(
                        'system',
                        '상담 연결이 종료되었습니다. (사용자 종료 또는 네트워크 연결 종료)',
                        Date.now()
                    );
                    state.doneCount = (state.doneCount || 0) + 1;
                    state.roomEnded = true;
                }

                // 아직 저장 안 됐으면 "저장 필요" 상태 유지
                if (!state.sessionSaved) {
                    markSessionNeedSave();
                }

                setRoomStatus(false);
                if (typeof state.setEndButtonEnabled === 'function') {
                    state.setEndButtonEnabled(false);
                }
                updateSummaryUI();
            };

            ws.onerror = function () {
                // 에러는 onclose에서 마무리
            };
        }

        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 대기 목록 "연결" 버튼
        // ─────────────────────────────────────────────────────────────
        function bindLobbyClickHandler() {
            document.addEventListener('click', function (ev) {
                const btn = ev.target.closest('[data-join-room]');
                if (!btn) return;
                const roomId = btn.getAttribute('data-join-room') || '';
                if (!roomId) return;

                // 이전 방이 종료됐는데 저장 안 했으면 막기
                if (state.currentRoom && state.roomEnded && !state.sessionSaved) {
                    alert('이전 상담의 기록을 먼저 저장해 주세요.');
                    return;
                }

                log('JOIN_ROOM_CLICK', roomId);

                // 대기 리스트에서 제거 (진행 중으로 간주) + 오른쪽 방 메타 세팅
                let req = null;
                if (state.pendingRooms[roomId]) {
                    req = state.pendingRooms[roomId];
                    setRoomMetaFromRequest(roomId, req);
                    delete state.pendingRooms[roomId];
                    renderLobbyList();
                }

                connectRoom(roomId);
            });
        }

        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 방 채팅 입력/전송 + 퀵 답변
        // ─────────────────────────────────────────────────────────────
        function bindRoomForm() {
            const form = $('#livechatRoomForm');
            const input = $('#livechatRoomInput');
            if (!form || !input) return;

            // 퀵 답변 버튼 (data-snippet) 처리
            const qrButtons = $$('.qr-btn', form);
            qrButtons.forEach((btn) => {
                btn.addEventListener('click', function () {
                    const snippet = btn.getAttribute('data-snippet') || btn.textContent || '';
                    if (!snippet) return;

                    const curr = String(input.value || '');
                    if (!curr) {
                        input.value = snippet;
                    } else {
                        if (!/\n$/.test(curr)) {
                            input.value = curr + '\n' + snippet;
                        } else {
                            input.value = curr + snippet;
                        }
                    }
                    input.focus();
                    try {
                        input.selectionStart = input.selectionEnd = input.value.length;
                    } catch (_) { }
                });
            });

            // Enter / Shift+Enter 처리
            input.addEventListener('keydown', function (ev) {
                if (ev.key === 'Enter') {
                    if (ev.shiftKey) {
                        // 줄바꿈
                        return;
                    }
                    ev.preventDefault();
                    form.requestSubmit();
                }
            });

            form.addEventListener('submit', function (ev) {
                ev.preventDefault();
                const msg = String(input.value || '').trim();
                if (!msg) return;

                // 이미 종료된 방이면 추가 전송 막기
                if (state.roomEnded) {
                    alert('이미 종료된 상담입니다. 다른 방을 선택해 주세요.');
                    return;
                }

                if (!state.roomSocket || state.roomSocket.readyState !== WebSocket.OPEN) {
                    alert('상담 방 WebSocket이 아직 연결되지 않았습니다.');
                    return;
                }

                const payload = {
                    sender: 'operator',
                    text: msg
                };

                try {
                    state.roomSocket.send(JSON.stringify(payload));
                    log('ROOM_SEND', payload);
                    input.value = '';
                    input.focus();
                    // 메시지는 서버 에코를 통해 appendRoomMessage로 렌더됨
                } catch (e) {
                    log('ROOM_SEND_ERR', String(e));
                    alert('메시지 전송 중 오류가 발생했습니다.');
                }
            });
        }

        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 상담 종료 버튼
        // ─────────────────────────────────────────────────────────────
        function bindEndButton() {
            const btn = $('#livechatEndBtn');
            if (!btn) return;

            function setEnabled(enabled) {
                btn.disabled = !enabled;
            }

            // 처음에는 비활성화
            setEnabled(false);
            state.setEndButtonEnabled = setEnabled;

            btn.addEventListener('click', function () {
                if (!state.roomSocket || state.roomSocket.readyState !== WebSocket.OPEN) {
                    alert('상담 방 WebSocket이 아직 연결되지 않았습니다.');
                    return;
                }

                if (!window.confirm('이 상담을 종료하시겠습니까?')) {
                    return;
                }

                endCurrentRoom();
            });
        }

        // ─────────────────────────────────────────────
        // 이벤트 바인딩: 상담 기록 저장 버튼
        // ─────────────────────────────────────────────
        function bindSessionSave() {
            sessionSaveBtn = $('#sessionSaveBtn');
            sessionStatusPill = $('#sessionStatusPill');

            // UI 자체가 없으면 스킵
            if (!sessionSaveBtn && !sessionStatusPill) {
                log('SESSION_SAVE_UI_MISSING', null);
                return;
            }

            // 초기 상태: "진행 중 · 저장 준비"
            if (sessionSaveBtn) {
                sessionSaveBtn.disabled = true;  // 상담 종료 전까지 비활성화
            }
            setSessionStatus('idle');

            if (!sessionSaveBtn) return;

            sessionSaveBtn.addEventListener('click', async function () {
                log('SESSION_SAVE_CLICK', {
                    currentRoom: state.currentRoom,
                    roomEnded: state.roomEnded,
                    sessionSaved: state.sessionSaved
                });

                if (!state.currentRoom) {
                    alert('현재 선택된 상담 방이 없습니다.');
                    return;
                }

                if (!state.roomEnded) {
                    alert('상담이 아직 진행 중입니다.\n상담 종료 후 기록을 저장해 주세요.');
                    return;
                }

                const typeEl = $('#sessionType');
                const noteEl = $('#sessionNote');
                const detailEl = $('#sessionDetail');

                const sessionType = typeEl ? String(typeEl.value || '') : '';
                const sessionNote = noteEl ? String(noteEl.value || '') : '';
                const sessionDetail = detailEl ? String(detailEl.value || '') : '';

                if (!sessionType && !sessionNote && !sessionDetail) {
                    alert('문의 유형 또는 세션 메모/상세 상담 기록 중 하나 이상을 입력해 주세요.');
                    return;
                }

                // 저장 시작 표시
                setSessionStatus('saving');
                sessionSaveBtn.disabled = true;

                const payload = {
                    room: state.currentRoom,
                    session_type: sessionType,
                    session_note: sessionNote,
                    session_detail: sessionDetail
                };

                // ✅ URL 결정: data-save-url > body data-livechat-save-url > 기본값
                const body = document.body || {};
                const bodyUrl =
                    body.dataset && body.dataset.livechatSaveUrl
                        ? body.dataset.livechatSaveUrl
                        : '';

                const saveUrl =
                    sessionSaveBtn.dataset.saveUrl ||
                    bodyUrl ||
                    '/api/livechat/save-session/';

                log('SESSION_SAVE_POST', { saveUrl, payload });

                try {
                    const res = await fetch(saveUrl, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken'),
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: JSON.stringify(payload)
                    });

                    if (!res.ok) {
                        const txt = await res.text().catch(() => '');
                        log('SESSION_SAVE_HTTP_ERR', { status: res.status, body: txt });
                        setSessionStatus('error');
                        sessionSaveBtn.disabled = false;
                        alert('상담 기록 저장에 실패했습니다.\n\n' + txt.slice(0, 200));
                        return;
                    }

                    const data = await res.json().catch(() => null);
                    log('SESSION_SAVE_RESP', data);

                    if (!data || data.ok === false) {
                        setSessionStatus('error');
                        sessionSaveBtn.disabled = false;
                        alert(
                            '상담 기록 저장에 실패했습니다.\n\n' +
                            (data && (data.error || data.message) || '서버 응답을 확인해 주세요.')
                        );
                        return;
                    }

                    // ✅ 성공
                    state.sessionSaved = true;
                    setSessionStatus('ok');
                    sessionSaveBtn.disabled = true;
                    alert('상담 기록을 저장했습니다.');

                    // 🔹 저장 직후에도 한번 즉시 최근 세션 새로고침
                    try {
                        refreshRecentSessions({ silent: false });
                    } catch (e) {
                        log('RECENT_SESS_AFTER_SAVE_ERR', e && e.message ? e.message : e);
                    }

                } catch (e) {
                    log('SESSION_SAVE_ERR', e && e.message ? e.message : e);
                    setSessionStatus('error');
                    sessionSaveBtn.disabled = false;
                    alert('상담 기록 저장 중 오류가 발생했습니다.');
                }
            });
        }



        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 오늘 세션 정리 버튼
        // ─────────────────────────────────────────────────────────────
        function bindCleanupButton() {
            const btn = $('#livechatCleanupBtn');
            if (!btn) return;

            btn.addEventListener('click', async function () {
                const url = btn.dataset.cleanupUrl || '/ragadmin/live-chat/cleanup/';
                if (!window.confirm('오늘 날짜 상담 세션 기록을 정리할까요?')) {
                    return;
                }

                try {
                    const res = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken'),
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: JSON.stringify({ mode: 'today' })
                    });

                    if (!res.ok) {
                        const txt = await res.text().catch(() => '');
                        alert('정리 요청 실패\n' + txt.slice(0, 200));
                        return;
                    }

                    const data = await res.json().catch(() => null);
                    if (!data || !data.ok) {
                        alert('정리 요청 실패: ' + (data && data.error ? data.error : '알 수 없는 오류'));
                        return;
                    }

                    // 최근 세션 리스트 비우기
                    const ul = $('.session-list');
                    if (ul) {
                        ul.innerHTML = '';
                    }
                    state.doneCount = 0;
                    updateSummaryUI();

                    // 정리 후에도 백엔드가 recent-url 을 제공하면 다시 한 번 새로고침
                    refreshRecentSessions({ silent: true }).catch(() => { });

                    alert('오늘 상담 세션 기록이 정리되었습니다.');
                } catch (e) {
                    log('CLEANUP_ERR', e && e.message ? e.message : e);
                    alert('정리 중 오류가 발생했습니다.');
                }
            });
        }

        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 최근 세션 클릭 → 오른쪽 메모/상세 채우기
        // ─────────────────────────────────────────────────────────────
        function bindHistoryClick() {
            const list = $('.session-list');
            if (!list) return;

            list.addEventListener('click', function (ev) {
                const delBtn = ev.target.closest('.session-delete-btn');
                if (delBtn) {
                    // 삭제 버튼은 다른 핸들러에서 처리
                    return;
                }

                const item = ev.target.closest('.session-item');
                if (!item) return;

                const shortNote = item.dataset.historyNote || '';
                const fullMemo = item.dataset.historyMemo || '';

                const noteInput = $('#sessionNote');
                const detailInput = $('#sessionDetail');

                if (noteInput) {
                    noteInput.value = shortNote;
                }
                if (detailInput) {
                    detailInput.value = fullMemo || shortNote;
                }
            });
        }

        // ─────────────────────────────────────────────────────────────
        // 이벤트 바인딩: 최근 세션 개별 삭제 버튼
        // ─────────────────────────────────────────────────────────────
        function bindHistoryDelete() {
            document.addEventListener('click', async function (ev) {
                const btn = ev.target.closest('.session-delete-btn');
                if (!btn) return;

                const li = btn.closest('.session-item');
                if (!li) return;

                const sessionId = btn.dataset.deleteSessionId || li.dataset.sessionId;
                const url = btn.dataset.deleteUrl || '/ragadmin/live-chat/cleanup/';
                if (!sessionId) return;

                if (!window.confirm('이 상담 세션 기록을 삭제할까요?')) {
                    return;
                }

                try {
                    const res = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken'),
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: JSON.stringify({ session_id: sessionId })
                    });

                    if (!res.ok) {
                        const txt = await res.text().catch(() => '');
                        alert('삭제 실패\n' + txt.slice(0, 200));
                        return;
                    }

                    const data = await res.json().catch(() => null);
                    if (!data || !data.ok) {
                        alert('삭제 실패: ' + (data && data.error ? data.error : '알 수 없는 오류'));
                        return;
                    }

                    li.remove();
                } catch (e) {
                    log('HISTORY_DELETE_ERR', e && e.message ? e.message : e);
                    alert('삭제 중 오류가 발생했습니다.');
                }
            });
        }

        // ─────────────────────────────────────────────────────────────
        // 초기화
        // ─────────────────────────────────────────────────────────────
        function init() {
            const body = document.body;

            // body data-attribute와 URL 쿼리 파라미터 같이 참고
            let initialRoomAttr = (body && body.dataset && body.dataset.initialRoom) || '';
            let roomFromQuery = '';
            let autoJoin = false;

            try {
                const sp = new URLSearchParams(window.location.search || '');
                roomFromQuery = sp.get('room') || sp.get('session_id') || '';
                autoJoin = sp.get('autojoin') === '1';
            } catch (_) { }

            const initialRoom = roomFromQuery || initialRoomAttr || 'master';
            log('INIT', { initialRoom, autoJoin });

            // 알림 오디오 준비
            notifyAudio = $('#livechatNotifySound') || null;

            // 로비 WebSocket 연결
            connectLobby();

            // 각종 이벤트 바인딩
            bindLobbyClickHandler();
            bindRoomForm();
            bindEndButton();
            bindSessionSave();
            bindCleanupButton();
            bindHistoryClick();
            bindHistoryDelete();

            // autojoin=1 이면 해당 방 자동 접속
            if (autoJoin && initialRoom && initialRoom !== 'master') {
                connectRoom(initialRoom);
            }

            // 최근 상담 세션 리스트가 Ajax 갱신을 지원한다면, 주기적으로 새로고침
            try {
                refreshRecentSessions({ silent: true });
                setInterval(() => {
                    refreshRecentSessions({ silent: true });
                }, 15000); // 15초마다
            } catch (e) {
                log('RECENT_SESS_INIT_ERR', e && e.message ? e.message : e);
            }

            updateSummaryUI();
        }

        document.addEventListener('DOMContentLoaded', init);
    } catch (e) {
        try {
            if (typeof window.dglog === 'function') {
                window.dglog('LIVECHAT_ADMIN_FATAL', e);
            } else {
                console.error('[livechat_admin fatal]', e);
            }
        } catch (_) { }
    }
})();
