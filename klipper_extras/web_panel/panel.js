(function() {
    'use strict';

    let ws = null;
    let mmuState = null;
    let editingTool = null;
    let requestId = 1;

    const elements = {
        connectionStatus: document.getElementById('connection-status'),
        stateText: document.getElementById('state-text'),
        activeTool: document.getElementById('active-tool'),
        boxesContainer: document.getElementById('boxes-container'),
        btnHome: document.getElementById('btn-home'),
        btnUnload: document.getElementById('btn-unload'),
        editModal: document.getElementById('edit-modal'),
        modalTitle: document.getElementById('modal-title'),
        modalClose: document.getElementById('modal-close'),
        modalCancel: document.getElementById('modal-cancel'),
        modalSave: document.getElementById('modal-save'),
        colorPicker: document.getElementById('color-picker'),
        colorInput: document.getElementById('color-input'),
        materialSelect: document.getElementById('material-select')
    };

    function connect() {
        // Try same origin first (works with nginx proxy), fall back to port 7125
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.hostname;
        const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');

        // Moonraker WebSocket paths to try
        const wsUrls = [
            `${protocol}//${host}:${port}/websocket`,      // Via nginx proxy
            `${protocol}//${host}:7125/websocket`          // Direct Moonraker
        ];

        tryConnect(wsUrls, 0);
    }

    function tryConnect(urls, index) {
        if (index >= urls.length) {
            console.error('All WebSocket connection attempts failed');
            setTimeout(() => tryConnect(urls, 0), 5000);
            return;
        }

        const wsUrl = urls[index];
        console.log('Trying WebSocket:', wsUrl);
        ws = new WebSocket(wsUrl);

        ws.onopen = function() {
            console.log('WebSocket connected');
            elements.connectionStatus.textContent = 'Connected';
            elements.connectionStatus.classList.remove('disconnected');
            elements.connectionStatus.classList.add('connected');
            subscribe();
        };

        ws.onclose = function() {
            elements.connectionStatus.textContent = 'Disconnected';
            elements.connectionStatus.classList.remove('connected');
            elements.connectionStatus.classList.add('disconnected');
            // Try next URL or restart from beginning
            setTimeout(() => tryConnect(urls, index + 1), 1000);
        };

        ws.onerror = function(err) {
            console.error('WebSocket error:', err);
            ws.close();
        };

        ws.onmessage = function(event) {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error('Failed to parse message:', e);
            }
        };
    }

    function subscribe() {
        ws.send(JSON.stringify({
            jsonrpc: '2.0',
            method: 'printer.objects.subscribe',
            params: { objects: { pico_mmu: null } },
            id: requestId++
        }));

        ws.send(JSON.stringify({
            jsonrpc: '2.0',
            method: 'printer.objects.query',
            params: { objects: { pico_mmu: null } },
            id: requestId++
        }));
    }

    function handleMessage(msg) {
        if (msg.method === 'notify_status_update') {
            const status = msg.params[0];
            if (status.pico_mmu) {
                updateState(status.pico_mmu);
            }
        } else if (msg.result && msg.result.status && msg.result.status.pico_mmu) {
            updateState(msg.result.status.pico_mmu);
        }
    }

    function updateState(state) {
        mmuState = state;
        elements.stateText.textContent = state.state || '-';
        elements.activeTool.textContent = state.current_tool >= 0 ? `T${state.current_tool}` : 'None';
        renderBoxes();
    }

    function renderBoxes() {
        if (!mmuState || !mmuState.boxes) return;

        let html = '';
        for (const box of mmuState.boxes) {
            const boxTools = mmuState.tools.filter(t => box.slots.includes(t.tool));
            const statusIcon = box.online ? '&#10003;' : '&#10007;';
            const statusClass = box.online ? 'online' : 'offline';

            html += `<div class="box">
                <div class="box-header ${statusClass}">
                    <span>Box ${box.addr}</span>
                    <span class="box-status">${statusIcon} ${box.online ? 'online' : 'offline'}</span>
                </div>
                <div class="slots-grid">`;

            for (const tool of boxTools) {
                html += renderSlot(tool);
            }

            html += `</div></div>`;
        }

        elements.boxesContainer.innerHTML = html;
        attachSlotListeners();
    }

    function renderSlot(tool) {
        const isActive = mmuState.current_tool === tool.tool;
        const stateClass = getStateClass(tool.state);
        const colorHex = tool.color || '808080';

        return `<div class="slot ${stateClass} ${isActive ? 'active' : ''}" data-tool="${tool.tool}">
            <div class="slot-color" style="background-color: #${colorHex}"></div>
            <div class="slot-info">
                <span class="slot-name">T${tool.tool}</span>
                <span class="slot-material">${tool.material || '-'}</span>
                <span class="slot-state">${tool.state}</span>
                ${tool.group ? `<span class="slot-group">grp ${tool.group}</span>` : ''}
            </div>
            <button class="btn-edit" data-tool="${tool.tool}" title="Edit filament">&#9998;</button>
        </div>`;
    }

    function getStateClass(state) {
        switch (state) {
            case 'LOADED': return 'state-loaded';
            case 'FEEDING':
            case 'ASSIST': return 'state-active';
            case 'RETRACTING': return 'state-active';
            case 'ERROR': return 'state-error';
            case 'EMPTY':
            default: return 'state-empty';
        }
    }

    function attachSlotListeners() {
        document.querySelectorAll('.slot').forEach(slot => {
            slot.addEventListener('click', function(e) {
                if (e.target.classList.contains('btn-edit')) return;
                const tool = this.dataset.tool;
                sendGcode(`MMU_CHANGE_TOOL TOOL=${tool}`);
            });
        });

        document.querySelectorAll('.btn-edit').forEach(btn => {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                openEditModal(parseInt(this.dataset.tool));
            });
        });
    }

    function sendGcode(script) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            console.error('WebSocket not connected');
            return;
        }

        ws.send(JSON.stringify({
            jsonrpc: '2.0',
            method: 'printer.gcode.script',
            params: { script: script },
            id: requestId++
        }));
    }

    function openEditModal(toolNum) {
        editingTool = toolNum;
        const tool = mmuState.tools.find(t => t.tool === toolNum);
        if (!tool) return;

        elements.modalTitle.textContent = `Edit T${toolNum} Filament`;
        elements.colorInput.value = tool.color || '';
        elements.colorPicker.value = '#' + (tool.color || '808080');
        elements.materialSelect.value = tool.material || '';
        elements.editModal.classList.remove('hidden');
    }

    function closeEditModal() {
        elements.editModal.classList.add('hidden');
        editingTool = null;
    }

    function saveFilament() {
        if (editingTool === null) return;

        let color = elements.colorInput.value.replace('#', '').toUpperCase();
        if (!/^[0-9A-F]{6}$/.test(color)) {
            color = elements.colorPicker.value.replace('#', '').toUpperCase();
        }

        const material = elements.materialSelect.value;
        let cmd = `MMU_SET_FILAMENT TOOL=${editingTool} COLOR=${color}`;
        if (material) {
            cmd += ` MATERIAL=${material}`;
        }

        sendGcode(cmd);
        closeEditModal();
    }

    elements.colorPicker.addEventListener('input', function() {
        elements.colorInput.value = this.value.replace('#', '').toUpperCase();
    });

    elements.colorInput.addEventListener('input', function() {
        const val = this.value.replace('#', '');
        if (/^[0-9A-Fa-f]{6}$/.test(val)) {
            elements.colorPicker.value = '#' + val;
        }
    });

    elements.btnHome.addEventListener('click', () => sendGcode('MMU_HOME'));
    elements.btnUnload.addEventListener('click', () => sendGcode('MMU_UNLOAD'));
    elements.modalClose.addEventListener('click', closeEditModal);
    elements.modalCancel.addEventListener('click', closeEditModal);
    elements.modalSave.addEventListener('click', saveFilament);

    elements.editModal.addEventListener('click', function(e) {
        if (e.target === this) closeEditModal();
    });

    connect();
})();
