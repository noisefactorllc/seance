import { peerColor } from './peerColors.js'
import { applyTextEdit, diffText, rebaseTextWithLocalEdit, transformSelection } from './textOps.js'

const DEFAULT_DOC_ID = 'main'
const DEFAULT_DIALECT = 'noisemaker-dsl'
const DEFAULT_PROPOSAL_THROTTLE_MS = 110
const DEFAULT_CURSOR_THROTTLE_MS = 80
const DEFAULT_NODE_THROTTLE_MS = 120
const DEFAULT_INFLIGHT_TIMEOUT_MS = 10000
const DEFAULT_INFLIGHT_RETRANSMITS = 3
const DEFAULT_RETRY_AFTER_MS = 250
// Bound edit text by both UTF-16 length and encoded JSON bytes. A control
// character or lone surrogate can require six wire bytes (a \uXXXX escape).
const MAX_EDIT_CHARS = 15000
const MAX_EDIT_JSON_BYTES = 60000
// protocol.md section 7. Terminal codes mean "do not come back on your own".
const CLOSE_KINDS = {
    4400: 'protocol',
    4401: 'kicked',
    4403: 'forbidden',
    4404: 'unknown-session',
    4408: 'slow-consumer',
    4409: 'dialect-mismatch',
    4423: 'locked',
    4429: 'limit',
}
const TERMINAL_CLOSE_CODES = new Set([4400, 4401, 4403, 4404, 4409, 4423])

export function createOnlineDslLayer(options = {}) {
    return new OnlineDslLayer(options)
}

class OnlineDslLayer {
    constructor(options) {
        this.options = {
            protocol: 1,
            defaultDocId: DEFAULT_DOC_ID,
            dialect: DEFAULT_DIALECT,
            proposalThrottleMs: DEFAULT_PROPOSAL_THROTTLE_MS,
            cursorThrottleMs: DEFAULT_CURSOR_THROTTLE_MS,
            nodeThrottleMs: DEFAULT_NODE_THROTTLE_MS,
            reconnectBaseMs: 500,
            reconnectMaxMs: 8000,
            reconnectJitter: 0.25,
            inFlightTimeoutMs: DEFAULT_INFLIGHT_TIMEOUT_MS,
            inFlightRetransmits: DEFAULT_INFLIGHT_RETRANSMITS,
            urlParam: 'seance',
            stripUrlParams: ['code'],
            ...options,
        }
        this.options.dialects = this.options.dialects || [this.options.dialect]
        this.fetch = this.options.fetch || globalThis.fetch?.bind(globalThis)
        this.WebSocket = this.options.WebSocket || globalThis.WebSocket
        this.docs = new Map()
        this.listeners = new Map()
        this.status = 'offline'
        this.sessionId = null
        this.sessionDialect = null
        this.activeDefaultDocId = this.options.defaultDocId
        this.socket = null
        this.user = null
        this.readonly = false
        this._anonTokenStorage = anonTokenStorage(this.options)
        this._anonTokenStorageKey = anonTokenStorageKey(this._httpBaseUrl())
        this.anonToken = null
        this._rememberAnonToken(this.options.anonToken || this._readAnonToken())
        this.lastSeq = 0
        this.nodes = new Map()
        this.polyRev = 0
        this._connectDeferred = null
        this._intentionalDisconnect = true
        this._connectedAtLeastOnce = false
        this._reconnectTimer = null
        this._reconnectAttempts = 0
        this._reconnecting = false
        this._lastConnectError = null
        this._nodeQueue = []
        this._nodePending = new Map()
        this._nodeSendTimer = null
        this._nodeInFlightTimer = null
        this._lastNodeSendAt = 0
        this._nodeAuthorSeq = 0
        this._lastProposalAt = 0
        this._resyncPending = false
        this._connectionGeneration = 0
    }

    on(eventName, handler) {
        const set = this.listeners.get(eventName) || new Set()
        set.add(handler)
        this.listeners.set(eventName, set)
        return () => set.delete(handler)
    }

    connect(sessionId = this.sessionId, options = {}) {
        if (!sessionId) {
            throw new Error('connect requires a session id')
        }
        if (!options.reconnect) {
            this._connectionGeneration += 1
            this._resetSessionWrites()
        }
        if (this._connectDeferred) {
            this._connectDeferred.reject(new Error('connection superseded'))
            this._connectDeferred = null
        }
        clearTimeout(this._reconnectTimer)
        this._reconnectTimer = null
        const previousSocket = this.socket
        if (previousSocket) {
            this.socket = null
            previousSocket.close()
        }
        this.sessionId = sessionId
        this._intentionalDisconnect = false
        this._reconnecting = Boolean(options.reconnect)
        if (!this._reconnecting) this._connectedAtLeastOnce = false
        this._lastConnectError = null
        const Socket = this.WebSocket
        if (!Socket) {
            this._setStatus('offline')
            throw new Error('WebSocket is not available')
        }
        this._setStatus('connecting')

        let socket
        try {
            socket = new Socket(this._wsUrl(sessionId))
        } catch (error) {
            this._setStatus('offline')
            throw error
        }
        this.socket = socket
        this._connectDeferred = deferred()

        addSocketListener(socket, 'open', () => {
            if (this.socket !== socket) return
            this._send({
                type: 'hello',
                protocol: this.options.protocol,
                dialects: this.options.dialects,
                ...(this.anonToken ? { anon_token: this.anonToken } : {}),
                ...(this.lastSeq ? { resume: { last_seq: this.lastSeq } } : {}),
            })
        })
        addSocketListener(socket, 'message', (event) => {
            if (this.socket !== socket) return
            let msg
            try {
                msg = JSON.parse(event.data)
            } catch (error) {
                const fault = clientError('bad_frame', 'unparseable frame from server', error)
                if (this._connectDeferred) {
                    // A poisoned welcome/snapshot must fail the join, not hang it.
                    this._lastConnectError = fault
                    this._connectDeferred.reject(fault)
                    this._connectDeferred = null
                }
                this._emit('error', fault)
                return
            }
            try {
                this._handleMessage(msg)
            } catch (error) {
                this._handleClientFault(msg, error)
            }
        })
        addSocketListener(socket, 'close', (event) => {
            if (this.socket !== socket) return
            this.socket = null
            const code = Number(event?.code) || 0
            const reason = String(event?.reason || '')
            const kind = CLOSE_KINDS[code] || null
            const terminal = TERMINAL_CLOSE_CODES.has(code)
            const hadConnected = this._connectedAtLeastOnce
            if (this._lastConnectError && this._lastConnectError.closeCode === undefined) {
                // The error frame already rejected connect(); give that Error the close code too.
                this._lastConnectError.closeCode = code
            }
            if (this._connectDeferred) {
                const error = this._lastConnectError || new Error(reason || 'connection closed before session snapshot')
                if (error.closeCode === undefined) error.closeCode = code
                if (kind && !error.code) error.code = kind
                this._connectDeferred.reject(error)
                this._connectDeferred = null
            }
            const willReconnect = !this._intentionalDisconnect && hadConnected && Boolean(this.sessionId) && !terminal
            if (!this._intentionalDisconnect) {
                this._emit('disconnect', { code, reason, kind, willReconnect, attempt: this._reconnectAttempts })
            }
            if (willReconnect) {
                this._scheduleReconnect()
                return
            }
            if (terminal) this._intentionalDisconnect = true
            this._clearInFlight()
            if (this.status !== 'offline') this._setStatus('offline')
        })
        addSocketListener(socket, 'error', (event) => {
            if (this.socket !== socket) return
            this._lastConnectError = event instanceof Error ? event : new Error('websocket error')
            if (this._connectDeferred) {
                this._connectDeferred.reject(this._lastConnectError)
                this._connectDeferred = null
            }
            this._emit('error', event)
        })

        return this._connectDeferred.promise
    }

    disconnect() {
        this._connectionGeneration += 1
        this._intentionalDisconnect = true
        clearTimeout(this._reconnectTimer)
        this._reconnectTimer = null
        this._stopInFlightTimers()
        if (this._connectDeferred) {
            this._connectDeferred.reject(new Error('connection closed'))
            this._connectDeferred = null
        }
        if (this.socket) {
            this.socket.close()
            this.socket = null
        }
        this._setStatus('offline')
    }

    async takeOnline(seed = null) {
        if (!this.fetch) throw new Error('fetch is not available')
        const generation = ++this._connectionGeneration
        const snapshot = this._normalizeSeed(seed)
        const response = await this.fetch(`${this._httpBaseUrl()}/v1/sessions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(this.anonToken ? { 'X-Seance-Anon': this.anonToken } : {}),
            },
            credentials: 'include',
            body: JSON.stringify({ snapshot, dialect: this.options.dialect }),
        })
        if (generation !== this._connectionGeneration) throw new Error('connection superseded')
        if (!response.ok) {
            throw new Error(`failed to create seance session (${response.status})`)
        }
        const body = await response.json()
        if (generation !== this._connectionGeneration) throw new Error('connection superseded')
        this._rememberAnonToken(body.anon_token)
        return this.connect(body.session_id)
    }

    joinSession(sessionId) {
        return this.connect(sessionId)
    }

    async createOrJoinSession(seed = null) {
        const existing = this.readSessionFromUrl(this.options.location || globalThis.location)
        return existing ? this.joinSession(existing) : this.takeOnline(seed)
    }

    goOffline() {
        this.disconnect()
        this.readonly = false
        this._setReadOnly(false)
        for (const doc of this.docs.values()) {
            clearTimeout(doc.proposalTimer)
            clearTimeout(doc.cursorTimer)
            this._clearDocInFlight(doc)
            doc.queuedText = null
            doc.holdText = null
            doc.proposalTimer = null
            doc.cursorTimer = null
            clearRemoteSelections(doc.binding)
        }
        // Nothing from the previous session applies to the next one.
        this.lastSeq = 0
        this._reconnectAttempts = 0
        this._resyncPending = false
        clearTimeout(this._nodeSendTimer)
        this._nodeSendTimer = null
        this._nodeQueue = []
        this._nodePending.clear()
        this._emit('offline')
    }

    bindEditor(binding) {
        const normalized = normalizeBinding(binding, this.options.defaultDocId)
        const doc = this._ensureDoc(normalized.docId)
        doc.cleanupBinding?.()
        doc.binding = normalized
        doc.features = detectHandfishFeatures(normalized.editor)
        const current = readEditorText(normalized)
        doc.text = current
        if (doc.rev === 0 && doc.serverText === '') {
            doc.serverText = current
        }

        const inputHandler = (event) => {
            if (event?.target && event.target !== normalized.editor) return
            const next = readEditorText(normalized)
            this.updateLocalText(normalized.docId, next, {
                source: 'editor',
                detail: event?.detail || null,
            })
        }
        const selectionHandler = (event) => {
            this._queueCursor(normalized.docId, event?.detail || null)
        }
        normalized.editor?.addEventListener?.('input', inputHandler)
        normalized.editor?.addEventListener?.('selectionchange', selectionHandler)
        doc.cleanupBinding = () => {
            normalized.editor?.removeEventListener?.('input', inputHandler)
            normalized.editor?.removeEventListener?.('selectionchange', selectionHandler)
        }
        return () => this.unbindEditor(normalized.docId)
    }

    unbindEditor(docId = this.options.defaultDocId) {
        const doc = this.docs.get(this._resolveDocId(docId))
        if (!doc) return
        doc.cleanupBinding?.()
        clearRemoteSelections(doc.binding)
        doc.binding = null
        doc.cleanupBinding = null
    }

    updateLocalText(docId = this.options.defaultDocId, text, meta = {}) {
        const doc = this._ensureDoc(this._resolveDocId(docId))
        const next = String(text ?? '')
        if (doc.text === next) return null
        if (this.readonly) {
            this._emit('readonly-write', { docId, text: next, meta })
            return null
        }
        const validation = validateBindingText(doc.binding, next, { docId, meta, source: meta.source || 'local' })
        if (!validation.ok) {
            this._emit('validation-error', {
                docId,
                text: next,
                meta,
                reason: validation.reason,
            })
            return null
        }
        doc.text = next
        this._emit('local-text', { docId, text: next, meta })
        this._scheduleProposal(doc)
        return diffText(doc.serverText, doc.text)
    }

    upsertNode(id, { kind, text, parentId = null } = {}, { resubmit = true } = {}) {
        if (this.readonly) {
            this._emit('readonly-write', { op: 'upsert', node: { id, kind, text, parentId } })
            return
        }
        this._nodeQueue.push({ op: 'upsert', id, kind, text, parentId, resubmit, attempts: 0 })
        this._scheduleNodeSend()
    }

    deleteNode(id, { resubmit = true } = {}) {
        if (this.readonly) {
            this._emit('readonly-write', { op: 'delete', id })
            return
        }
        this._nodeQueue.push({ op: 'delete', id, resubmit, attempts: 0 })
        this._scheduleNodeSend()
    }

    getNodes() {
        return [...this.nodes.values()].map((node) => ({ ...node }))
    }

    getPendingNodeWrites() {
        return [...this._nodePending.values(), ...this._nodeQueue].map((item) => (
            item.op === 'upsert'
                ? { op: item.op, id: item.id, kind: item.kind, text: item.text, parentId: item.parentId }
                : { op: item.op, id: item.id }
        ))
    }

    getNodeRev() {
        return this.polyRev
    }

    getStatus() {
        return this.status
    }

    getSessionId() {
        return this.sessionId
    }

    getSessionDialect() {
        return this.sessionDialect
    }

    getShareUrl() {
        if (!this.sessionId) return null
        return this.writeSessionToUrl(
            this.options.publicAppUrl || this.options.location || globalThis.location,
            this.sessionId,
        )
    }

    readSessionFromUrl(locationLike) {
        if (!locationLike) return null
        return urlFrom(locationLike).searchParams.get(this.options.urlParam)
    }

    writeSessionToUrl(locationLike, sessionId) {
        const url = urlFrom(locationLike)
        for (const param of this.options.stripUrlParams || []) {
            url.searchParams.delete(param)
        }
        if (sessionId) {
            url.searchParams.set(this.options.urlParam, sessionId)
        } else {
            url.searchParams.delete(this.options.urlParam)
        }
        return url.toString()
    }

    _handleMessage(msg) {
        if (Number.isInteger(msg.seq)) this.lastSeq = Math.max(this.lastSeq, msg.seq)
        switch (msg.type) {
            case 'welcome':
                this.user = msg.you
                this.readonly = Boolean(msg.you?.readonly)
                this._setReadOnly(this.readonly)
                if (this.readonly) this._dropNodeWrites('readonly')
                this._rememberAnonToken(msg.anon_token)
                this.sessionDialect = msg.dialect ?? null
                this._emit('welcome', msg)
                break
            case 'session-snapshot':
                this._adoptDocs(msg.docs || [], { preserveLocal: this._reconnecting || this._resyncPending })
                this._adoptPoly(msg.poly)
                if (this._reconnecting) this._recoverNodeQueue()
                this._resyncPending = false
                this._setStatus(this.readonly ? 'readonly' : 'online')
                this._connectedAtLeastOnce = true
                this._reconnecting = false
                this._reconnectAttempts = 0
                this._scheduleDirtyDocs()
                this._resolveConnect(msg)
                break
            case 'doc-snapshot':
                // doc-create / doc-reset rebroadcast every document; leave the ones
                // that did not change alone so their in-flight and local text survive.
                this._adoptDocs(msg.docs || [], { skipUnchanged: true })
                break
            case 'doc-edit':
                this._receiveRemoteEdit(msg)
                break
            case 'doc-ack':
                this._receiveAck(msg)
                break
            case 'doc-reject':
                this._receiveReject(msg)
                break
            case 'doc-cursor':
                this._receiveCursor(msg)
                break
            case 'poly-token-upsert':
                this._receiveRemoteNodeUpsert(msg)
                break
            case 'poly-snapshot':
                this._adoptPoly(msg)
                break
            case 'poly-token-delete':
                this._receiveRemoteNodeDelete(msg)
                break
            case 'poly-ack':
                this._receivePolyAck(msg)
                break
            case 'poly-reject':
                this._receivePolyReject(msg)
                break
            case 'moderation':
                this._receiveModeration(msg)
                break
            case 'error':
                this._lastConnectError = protocolError(msg)
                if (this._connectDeferred) {
                    this._connectDeferred.reject(this._lastConnectError)
                    this._connectDeferred = null
                }
                this._handleProposalError(msg)
                this._emit('error', msg)
                break
        }
    }

    _adoptDocs(snapshots, options = {}) {
        if (!options.skipUnchanged) {
            for (const doc of this.docs.values()) doc.available = false
        }
        this._adoptServerDefaultDoc(snapshots)
        for (const snapshot of snapshots) {
            const doc = this._ensureDoc(snapshot.id)
            doc.available = true
            if (options.skipUnchanged && doc.rev === snapshot.rev && doc.serverText === snapshot.text) continue
            const oldServerText = doc.serverText
            const localText = doc.text
            const hasLocalText = options.preserveLocal && !this.readonly && (
                localText !== oldServerText ||
                Boolean(doc.inFlight) ||
                doc.queuedText !== null
            )
            // An edit that was in flight when the socket dropped may or may not have
            // landed before the drop. Decide before rebasing so it is neither applied
            // twice nor lost.
            const base = hasLocalText && doc.inFlight
                ? resolveReconnectBase(oldServerText, doc.inFlight.edit, snapshot.text)
                : oldServerText
            const inFlight = doc.inFlight
            if (options.preserveLocal && this.readonly
                && (localText !== oldServerText || inFlight || doc.queuedText !== null)) {
                doc.recoveryRequired = true
                doc.recoveryReason ||= 'readonly_draft'
            }
            doc.rev = snapshot.rev
            doc.serverText = snapshot.text
            this._clearDocInFlight(doc)
            doc.holdText = null
            if (base === null || doc.recoveryRequired) {
                // V1 has no reconnect receipt. Preserve unresolved reconnect
                // or permission-loss drafts without publishing a guessed merge.
                doc.text = localText
                doc.queuedText = null
                doc.holdText = localText
                doc.recoveryRequired = true
                doc.recoveryReason ||= 'reconnect_ambiguous'
                this._applyRemoteText(doc, localText, null,
                    doc.recoveryReason === 'readonly_draft' ? 'readonly-draft' : 'reconnect-ambiguous')
                this._emit('doc-reject', {
                    type: 'doc-reject', docId: doc.docId,
                    baseRev: inFlight?.baseRev ?? doc.rev, authorSeq: inFlight?.authorSeq ?? null,
                    reason: doc.recoveryReason, snapshot,
                })
            } else if (hasLocalText) {
                const snapshotEdit = diffText(base, snapshot.text)
                doc.text = snapshotEdit
                    ? rebaseTextWithLocalEdit(base, localText, snapshotEdit)
                    : localText
                doc.queuedText = doc.text
                this._applyRemoteText(doc, doc.text, null, 'snapshot-rebase')
            } else {
                doc.text = snapshot.text
                doc.queuedText = null
                this._applyRemoteText(doc, snapshot.text, null, 'snapshot')
            }
        }
        this._emit('snapshot', { docs: snapshots })
    }

    _adoptServerDefaultDoc(snapshots) {
        const serverDefault = snapshots.find((snapshot) => snapshot.default) || snapshots[0]
        if (!serverDefault || serverDefault.id === this.activeDefaultDocId) return

        const placeholder = this.docs.get(this.activeDefaultDocId)
        const binding = placeholder?.binding
        if (!binding?._usesDefaultDocId) {
            this.activeDefaultDocId = serverDefault.id
            return
        }

        const target = this._ensureDoc(serverDefault.id)
        if (target.binding && target.binding !== binding) {
            this.activeDefaultDocId = serverDefault.id
            return
        }

        binding.docId = serverDefault.id
        target.binding = binding
        target.features = placeholder.features
        target.cleanupBinding = placeholder.cleanupBinding

        clearTimeout(placeholder.proposalTimer)
        clearTimeout(placeholder.cursorTimer)
        clearTimeout(placeholder.inFlightTimer)
        this.docs.delete(placeholder.docId)
        this.activeDefaultDocId = serverDefault.id
    }

    _adoptPoly(poly) {
        if (!poly) return
        this.nodes = new Map((poly.nodes || []).map((node) => [node.id, {
            id: node.id,
            kind: node.kind,
            text: node.text,
            parentId: node.parentId ?? null,
            version: node.version,
        }]))
        this.polyRev = poly.rev || 0
        this._emit('node-snapshot', { rev: this.polyRev, nodes: this.getNodes() })
    }

    _recoverNodeQueue() {
        const recovered = [...this._nodePending.values()]
        this._nodePending.clear()
        for (const item of recovered) delete item.baseRev
        this._nodeQueue.unshift(...recovered)
        this._scheduleNodeSend()
    }

    _receiveRemoteEdit(msg) {
        const doc = this._ensureDoc(msg.docId)
        const oldServer = doc.serverText
        const edit = msg.edit
        const hadLocal = doc.text !== doc.serverText
        doc.serverText = applyTextEdit(doc.serverText, edit)
        doc.rev = msg.rev
        if (doc.recoveryRequired) {
            this._emit('remote-edit', { docId: msg.docId, edit, rev: msg.rev })
            return
        }
        doc.holdText = null
        let editorEdit = edit

        if (hadLocal) {
            doc.text = rebaseTextWithLocalEdit(oldServer, doc.text, edit)
            doc.queuedText = doc.text
            editorEdit = null
        } else {
            doc.text = doc.serverText
        }
        this._applyRemoteText(doc, doc.text, editorEdit, 'remote')
        if (hadLocal) this._scheduleProposal(doc)
        this._emit('remote-edit', { docId: msg.docId, edit, rev: msg.rev })
    }

    _receiveAck(msg) {
        const doc = this._ensureDoc(msg.docId)
        if (!doc.inFlight || doc.inFlight.authorSeq !== msg.authorSeq) return
        const inFlight = doc.inFlight
        const edit = msg.edit
        doc.serverText = applyTextEdit(doc.serverText, edit)
        doc.rev = msg.rev
        this._clearDocInFlight(doc)
        doc.holdText = null
        if (doc.queuedText !== null) {
            doc.text = doc.queuedText
            doc.queuedText = null
        } else if (doc.text === inFlight.desiredText) {
            doc.text = doc.serverText
        }
        this._scheduleProposal(doc)
        notifyAcceptedText(doc.binding, doc.serverText, {
            docId: msg.docId,
            source: 'ack',
            rev: msg.rev,
            authorSeq: msg.authorSeq,
        })
        this._emit('doc-ack', { ...msg, edit })
    }

    _receiveReject(msg) {
        const doc = this._ensureDoc(msg.docId)
        const oldServer = doc.serverText
        const localText = doc.text
        const hadLocal = localText !== oldServer || Boolean(doc.inFlight) || doc.queuedText !== null
        if (doc.recoveryRequired) {
            // A refusal for the revoked in-flight write can arrive after the
            // moderation event. It updates server state, never the saved draft.
            if (msg.snapshot) {
                doc.rev = msg.snapshot.rev
                doc.serverText = msg.snapshot.text
            }
            this._clearDocInFlight(doc)
            this._emit('doc-reject', msg)
            return
        }
        if (msg.snapshot) {
            doc.rev = msg.snapshot.rev
            doc.serverText = msg.snapshot.text
            if (hadLocal && !this.readonly) {
                const snapshotEdit = diffText(oldServer, msg.snapshot.text)
                doc.text = snapshotEdit
                    ? rebaseTextWithLocalEdit(oldServer, localText, snapshotEdit)
                    : localText
                doc.queuedText = doc.text
                this._applyRemoteText(doc, doc.text, null, 'reject-rebase')
            } else {
                doc.text = msg.snapshot.text
                this._applyRemoteText(doc, doc.text, null, 'reject-snapshot')
            }
        }
        this._clearDocInFlight(doc)
        if (!hadLocal || this.readonly) doc.queuedText = null
        if (msg.reason === 'stale') {
            this._scheduleProposal(doc)
        } else {
            // invalid / too_large / duplicate / unknown: the same proposal would be
            // refused again. Keep the local text visible but do not resend it until
            // it changes or the server state moves.
            doc.holdText = doc.text
        }
        this._emit('doc-reject', msg)
    }

    _receiveRemoteNodeUpsert(msg) {
        if (Number.isInteger(msg.rev)) this.polyRev = Math.max(this.polyRev, msg.rev)
        const node = {
            id: msg.id,
            kind: msg.kind,
            text: msg.text,
            parentId: msg.parentId ?? null,
            version: msg.version,
        }
        this.nodes.set(node.id, node)
        this._emit('remote-node', { op: 'upsert', node })
    }

    _receiveRemoteNodeDelete(msg) {
        if (Number.isInteger(msg.rev)) this.polyRev = Math.max(this.polyRev, msg.rev)
        const removed = this._removeNodeCascade(msg.id)
        this._emit('remote-node', { op: 'delete', id: msg.id, removed })
    }

    _removeNodeCascade(id) {
        const prefix = `${id}.`
        const removed = []
        for (const nodeId of this.nodes.keys()) {
            if (nodeId === id || nodeId.startsWith(prefix)) removed.push(nodeId)
        }
        for (const nodeId of removed) this.nodes.delete(nodeId)
        return removed
    }

    _receivePolyAck(msg) {
        if (Number.isInteger(msg.rev)) this.polyRev = Math.max(this.polyRev, msg.rev)
        const pending = this._nodePending.get(msg.author_seq)
        if (!pending) return
        clearTimeout(this._nodeInFlightTimer)
        this._nodeInFlightTimer = null
        this._nodePending.delete(msg.author_seq)
        if (pending.op === 'upsert') {
            for (const applied of msg.applied || []) {
                if (applied.id !== pending.id) continue
                this.nodes.set(pending.id, {
                    id: pending.id,
                    kind: pending.kind,
                    text: pending.text,
                    parentId: pending.parentId,
                    version: applied.version,
                })
            }
        } else {
            for (const applied of msg.applied || []) {
                this.nodes.delete(applied.id)
            }
        }
        this._scheduleNodeSend()
        this._emit('node-ack', { ...msg, id: pending.id, op: pending.op })
    }

    _receivePolyReject(msg) {
        const pending = this._nodePending.get(msg.author_seq)
        if (Number.isInteger(msg.rev)) this.polyRev = Math.max(this.polyRev, msg.rev)
        if (!pending) return
        clearTimeout(this._nodeInFlightTimer)
        this._nodeInFlightTimer = null
        this._nodePending.delete(msg.author_seq)
        if (msg.reason === 'stale' && pending.resubmit && pending.attempts < 3) {
            pending.baseRev = msg.rev
            this._nodeQueue.unshift(pending)
            this._scheduleNodeSend()
            return
        }
        this._emit('node-reject', { id: pending.id, reason: msg.reason, attempts: pending.attempts })
        this._scheduleNodeSend()
    }

    _receiveCursor(msg) {
        const doc = this.docs.get(msg.docId)
        const binding = doc?.binding
        const editor = binding?.editor
        if (!editor?.setRemoteSelection || msg.connectionId === this.options.connectionId) return
        editor.setRemoteSelection({
            id: msg.connectionId || msg.user,
            userId: msg.user,
            label: msg.username || msg.user,
            color: peerColor(msg.user),
            start: msg.range.start,
            end: msg.range.end,
            direction: msg.direction || 'forward',
        })
    }

    _receiveModeration(msg) {
        if (msg.action !== 'readonly') {
            this._emit('moderation', msg)
            return
        }
        const userId = this.user?.user_id || this.user?.id
        if (!userId || msg.target_user !== userId || typeof msg.detail?.readonly !== 'boolean') {
            this._emit('moderation', msg)
            return
        }
        this.readonly = msg.detail.readonly
        this._setReadOnly(this.readonly)
        if (this.readonly) {
            this._dropNodeWrites('readonly')
            for (const doc of this.docs.values()) {
                clearTimeout(doc.proposalTimer)
                doc.proposalTimer = null
                const hasDraft = doc.text !== doc.serverText || doc.inFlight || doc.queuedText !== null
                this._clearDocInFlight(doc)
                doc.queuedText = null
                if (hasDraft && !doc.recoveryRequired) {
                    doc.recoveryRequired = true
                    doc.recoveryReason = 'readonly_draft'
                    doc.holdText = doc.text
                    this._emit('doc-reject', {
                        type: 'doc-reject', docId: doc.docId, reason: 'readonly_draft',
                        snapshot: { id: doc.docId, text: doc.serverText, rev: doc.rev },
                    })
                }
                if (!doc.recoveryRequired) {
                    doc.text = doc.serverText
                    this._applyRemoteText(doc, doc.serverText, null, 'readonly')
                }
            }
        } else {
            this._scheduleDirtyDocs()
        }
        this._setStatus(this.readonly ? 'readonly' : 'online')
        this._emit('moderation', msg)
    }

    _scheduleDirtyDocs() {
        for (const doc of this.docs.values()) {
            this._scheduleProposal(doc)
        }
        this._scheduleNodeSend()
    }

    _scheduleReconnect() {
        if (this._reconnectTimer || !this.sessionId) return
        this._stopInFlightTimers()
        this._setStatus('connecting')
        const base = Math.max(0, Number(this.options.reconnectBaseMs) || 0)
        const max = Math.max(base, Number(this.options.reconnectMaxMs) || base)
        const jitter = Math.min(1, Math.max(0, Number(this.options.reconnectJitter) || 0))
        const spread = 1 - jitter + 2 * jitter * Math.random()
        const delay = Math.round(Math.min(max, base * (2 ** this._reconnectAttempts)) * spread)
        this._reconnectAttempts += 1
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null
            Promise.resolve()
                .then(() => this.connect(this.sessionId, { reconnect: true }))
                .catch((error) => {
                    // The socket 'error' listener or the error frame already emitted this one.
                    if (error !== this._lastConnectError) this._emit('error', error)
                })
        }, delay)
    }

    _scheduleProposal(doc) {
        if (!this.socket || this.status !== 'online' || this.readonly || doc.recoveryRequired) return
        if (!doc.available) {
            if (doc.text !== doc.serverText && doc.holdText === null) {
                doc.holdText = doc.text
                this._emit('doc-reject', {
                    type: 'doc-reject', docId: doc.docId, baseRev: doc.rev,
                    authorSeq: null, reason: 'missing_document', snapshot: null,
                })
            }
            return
        }
        if (doc.inFlight) {
            doc.queuedText = doc.text
            return
        }
        if (doc.proposalTimer) return
        // Pace per document and across documents: the proposal lane is one token
        // bucket per connection (protocol.md section 8), not one per document.
        const elapsed = Date.now() - Math.max(doc.lastProposalAt, this._lastProposalAt)
        const delay = Math.max(0, this.options.proposalThrottleMs - elapsed)
        doc.proposalTimer = setTimeout(() => {
            doc.proposalTimer = null
            this._drainProposal(doc)
        }, delay)
    }

    _drainProposal(doc) {
        if (!doc.available || doc.recoveryRequired || !this.socket || this.status !== 'online' || this.readonly || doc.inFlight) return
        if (doc.text === doc.serverText) return
        if (doc.holdText !== null && doc.text === doc.holdText) return
        if (Date.now() - this._lastProposalAt < this.options.proposalThrottleMs) {
            this._scheduleProposal(doc)
            return
        }
        let edit = diffText(doc.serverText, doc.text)
        if (!edit) return
        let desiredText = doc.text
        const chunk = chunkEditText(edit.text)
        if (chunk.length < edit.text.length) {
            // Ship a large paste as a sequence of accepted chunks instead of one
            // frame the server refuses (max_frame / max_doc_edit_text).
            edit = { start: edit.start, end: edit.end, text: chunk }
            desiredText = applyTextEdit(doc.serverText, edit)
        }
        const authorSeq = ++doc.authorSeq
        const frame = {
            type: 'doc-edit',
            docId: doc.docId,
            baseRev: doc.rev,
            authorSeq,
            edit,
        }
        doc.inFlight = {
            authorSeq,
            baseRev: doc.rev,
            edit,
            desiredText,
            frame,
            retransmits: 0,
        }
        this._send(frame)
        const now = Date.now()
        doc.lastProposalAt = now
        this._lastProposalAt = now
        this._armInFlightTimer(doc)
    }

    _armInFlightTimer(doc, delay = this.options.inFlightTimeoutMs) {
        clearTimeout(doc.inFlightTimer)
        doc.inFlightTimer = null
        if (!(delay > 0)) return
        doc.inFlightTimer = setTimeout(() => {
            doc.inFlightTimer = null
            this._retransmitInFlight(doc)
        }, delay)
    }

    _retransmitInFlight(doc) {
        const inFlight = doc.inFlight
        if (!inFlight || !this.socket || this.status !== 'online') return
        const wait = this.options.proposalThrottleMs - (Date.now() - this._lastProposalAt)
        if (wait > 0) {
            this._armInFlightTimer(doc, wait)
            return
        }
        if (inFlight.retransmits >= this.options.inFlightRetransmits) {
            // No ack, reject, or error after repeated resends: the socket is dead or
            // the server is not answering. Drop it and let the reconnect path rebase
            // against a fresh snapshot.
            this.socket.close()
            return
        }
        inFlight.retransmits += 1
        // Same authorSeq: the server answers a retransmit from its retry cache, so an
        // edit that did land is acked again rather than applied twice.
        this._send(inFlight.frame)
        this._lastProposalAt = Date.now()
        this._armInFlightTimer(doc)
    }

    _handleProposalError(msg) {
        if (msg?.code === 'rate_limited') {
            // The frame was dropped, not applied (protocol.md section 8). Resend the
            // in-flight proposals after retry_after with their original authorSeq.
            const seconds = Number(msg.retry_after)
            const delay = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : DEFAULT_RETRY_AFTER_MS
            for (const doc of this.docs.values()) {
                if (doc.inFlight) this._armInFlightTimer(doc, delay)
            }
            // Poly retransmits can be silently deduplicated by v1. If an
            // outstanding node remains unanswered after the rate delay, recover
            // it from a fresh snapshot instead of leaving it pending forever.
            if (this._nodePending.size) this._armNodeInFlightTimer(delay)
            return
        }
        if (['too_large', 'bad_frame', 'readonly', 'forbidden'].includes(msg?.code) &&
            (!msg.ref_type || msg.ref_type === 'poly-token-upsert' || msg.ref_type === 'poly-token-delete')) {
            clearTimeout(this._nodeInFlightTimer)
            this._nodeInFlightTimer = null
            const rejected = [...this._nodePending.values()]
            this._nodePending.clear()
            for (const item of rejected) this._emit('node-reject', { id: item.id, reason: msg.code, attempts: item.attempts })
            this._scheduleNodeSend()
        }
        if (msg?.code === 'too_large' && (!msg.ref_type || msg.ref_type === 'doc-edit')) {
            for (const doc of this.docs.values()) {
                if (!doc.inFlight) continue
                const { authorSeq, baseRev } = doc.inFlight
                this._clearDocInFlight(doc)
                doc.holdText = doc.text
                this._emit('doc-reject', {
                    type: 'doc-reject', docId: doc.docId, baseRev, authorSeq, reason: 'too_large', snapshot: null,
                })
            }
        }
    }

    _handleClientFault(msg, error) {
        this._emit('error', clientError(
            'client_desync',
            `failed to apply ${msg?.type || 'frame'}: ${error?.message || error}`,
            error,
            msg,
        ))
        if (this._resyncPending || !this.socket) return
        this._resyncPending = true
        this._send({ type: 'session-state' })
    }

    _clearDocInFlight(doc) {
        clearTimeout(doc.inFlightTimer)
        doc.inFlightTimer = null
        doc.inFlight = null
    }

    _stopInFlightTimers() {
        clearTimeout(this._nodeInFlightTimer)
        this._nodeInFlightTimer = null
        for (const doc of this.docs.values()) {
            clearTimeout(doc.inFlightTimer)
            doc.inFlightTimer = null
        }
    }

    _clearInFlight() {
        for (const doc of this.docs.values()) this._clearDocInFlight(doc)
    }

    _resetSessionWrites() {
        this.lastSeq = 0
        this._resyncPending = false
        this._reconnectAttempts = 0
        this._lastProposalAt = 0
        clearTimeout(this._nodeSendTimer)
        clearTimeout(this._nodeInFlightTimer)
        this._nodeInFlightTimer = null
        this._nodeSendTimer = null
        this._nodeQueue = []
        this._nodePending.clear()
        this._lastNodeSendAt = 0
        this.nodes.clear()
        this.polyRev = 0
        this.sessionDialect = null
        for (const doc of this.docs.values()) {
            clearTimeout(doc.proposalTimer)
            clearTimeout(doc.cursorTimer)
            this._clearDocInFlight(doc)
            doc.proposalTimer = null
            doc.cursorTimer = null
            doc.queuedText = null
            doc.holdText = null
            doc.available = false
            doc.recoveryRequired = false
            doc.recoveryReason = null
            if (this.sessionId) clearRemoteSelections(doc.binding)
        }
    }

    _scheduleNodeSend() {
        if (!this.socket || this.status !== 'online' || this.readonly) return
        if (this._nodePending.size) return
        if (this._nodeQueue.length === 0) return
        if (this._nodeSendTimer) return
        const elapsed = Date.now() - Math.max(this._lastNodeSendAt, this._lastProposalAt)
        const delay = Math.max(0, Math.max(this.options.nodeThrottleMs, this.options.proposalThrottleMs) - elapsed)
        this._nodeSendTimer = setTimeout(() => {
            this._nodeSendTimer = null
            this._drainNodeQueue()
        }, delay)
    }

    _drainNodeQueue() {
        if (!this.socket || this.status !== 'online' || this.readonly) return
        if (this._nodePending.size) return
        const throttle = Math.max(this.options.nodeThrottleMs, this.options.proposalThrottleMs)
        if (Date.now() - this._lastProposalAt < throttle) {
            this._scheduleNodeSend()
            return
        }
        const item = this._nodeQueue.shift()
        if (!item) return
        item.attempts += 1
        const authorSeq = ++this._nodeAuthorSeq
        const baseRev = item.baseRev !== undefined ? item.baseRev : this._resolveNodeBaseRev(item.id)
        item.authorSeq = authorSeq
        this._nodePending.set(authorSeq, item)
        if (item.op === 'upsert') {
            this._send({
                type: 'poly-token-upsert',
                base_rev: baseRev,
                id: item.id,
                kind: item.kind,
                text: item.text,
                parentId: item.parentId,
                author_seq: authorSeq,
            })
        } else {
            this._send({
                type: 'poly-token-delete',
                base_rev: baseRev,
                id: item.id,
                author_seq: authorSeq,
            })
        }
        this._lastNodeSendAt = Date.now()
        this._lastProposalAt = this._lastNodeSendAt
        if (this._nodePending.has(authorSeq)) this._armNodeInFlightTimer()
        if (this._nodeQueue.length > 0) this._scheduleNodeSend()
    }

    _armNodeInFlightTimer(delay = this.options.inFlightTimeoutMs) {
        clearTimeout(this._nodeInFlightTimer)
        this._nodeInFlightTimer = null
        if (!(delay > 0)) return
        this._nodeInFlightTimer = setTimeout(() => {
            this._nodeInFlightTimer = null
            if (this._nodePending.size && this.socket && this.status === 'online') this.socket.close()
        }, delay)
    }

    _dropNodeWrites(reason) {
        clearTimeout(this._nodeSendTimer)
        clearTimeout(this._nodeInFlightTimer)
        this._nodeSendTimer = null
        this._nodeInFlightTimer = null
        const rejected = [...this._nodePending.values(), ...this._nodeQueue]
        this._nodePending.clear()
        this._nodeQueue = []
        for (const item of rejected) this._emit('node-reject', { id: item.id, reason, attempts: item.attempts })
    }

    _resolveNodeBaseRev(id) {
        const tracked = this.nodes.get(id)
        return tracked ? tracked.version : this.polyRev
    }

    _queueCursor(docId, detail) {
        const doc = this.docs.get(docId)
        if (!doc || !this.socket || (this.status !== 'online' && this.status !== 'readonly')) return
        doc.lastSelection = detail || readSelection(doc.binding)
        if (doc.cursorTimer) return
        doc.cursorTimer = setTimeout(() => {
            doc.cursorTimer = null
            const selection = doc.lastSelection || readSelection(doc.binding)
            if (!selection) return
            this._send({
                type: 'doc-cursor',
                docId,
                range: { start: selection.start, end: selection.end },
                direction: selection.direction || 'none',
            })
        }, this.options.cursorThrottleMs)
    }

    _applyRemoteText(doc, text, edit = null, source = 'remote') {
        const binding = doc.binding
        if (!binding) return
        const editor = binding.editor
        const selection = readSelection(binding)
        if (edit && editor?.applyTextEdit) {
            if (editor.applyTextEdit(edit, { source: 'remote' })?.deferred) return
        } else if (edit && editor?.replaceRange) {
            if (editor.replaceRange(edit.start, edit.end, edit.text, { source: 'remote' })?.deferred) return
        } else {
            writeEditorText(binding, text)
            // A full-value setter can defer during composition without being
            // able to return a result. Only notify after it applied the value.
            if (readEditorText(binding) !== text) return
        }
        if (selection && editor?.setSelectionRange && edit) {
            const next = transformSelection(selection, edit)
            editor.setSelectionRange(next.start, next.end, next.direction)
        }
        notifyRemoteText(binding, text, {
            docId: doc.docId,
            source,
            edit,
            rev: doc.rev,
        })
    }

    _normalizeSeed(seed) {
        if (seed == null || Array.isArray(seed)) {
            return { docs: this._normalizeSeedDocs(seed) }
        }
        const snapshot = {}
        if (seed.docs !== undefined) snapshot.docs = this._normalizeSeedDocs(seed.docs)
        if (seed.poly !== undefined) snapshot.poly = this._normalizePolySeed(seed.poly)
        return snapshot
    }

    _normalizeSeedDocs(seedDocs) {
        const docs = seedDocs || [...this.docs.values()].map((doc) => ({
            id: doc.docId,
            title: doc.title || doc.docId,
            kind: doc.kind || 'dsl',
            text: doc.text,
            default: doc.docId === this.options.defaultDocId,
        }))
        const list = Array.isArray(docs) ? docs : Object.entries(docs).map(([id, value]) => ({
            id,
            title: value.title || id,
            kind: value.kind || 'dsl',
            text: value.text ?? String(value ?? ''),
            default: Boolean(value.default),
        }))
        return list.map((doc, index) => ({
            id: doc.id || (index === 0 ? this.options.defaultDocId : `doc:${index}`),
            title: doc.title || doc.id || 'DSL',
            kind: doc.kind || 'dsl',
            text: String(doc.text ?? ''),
            default: doc.default ?? index === 0,
        }))
    }

    _normalizePolySeed(poly) {
        const normalized = {
            programText: String(poly?.programText ?? ''),
            nodes: (poly?.nodes || []).map((node) => ({
                id: node.id,
                kind: node.kind,
                text: node.text,
                parentId: node.parentId ?? null,
            })),
        }
        if (poly?.frame !== undefined) normalized.frame = poly.frame
        return normalized
    }

    _ensureDoc(docId) {
        const id = docId || this.options.defaultDocId
        let doc = this.docs.get(id)
        if (!doc) {
            doc = {
                docId: id,
                available: false,
                recoveryRequired: false,
                recoveryReason: null,
                rev: 0,
                text: '',
                serverText: '',
                authorSeq: 0,
                inFlight: null,
                inFlightTimer: null,
                holdText: null,
                queuedText: null,
                proposalTimer: null,
                cursorTimer: null,
                lastProposalAt: 0,
                binding: null,
                cleanupBinding: null,
                features: {},
            }
            this.docs.set(id, doc)
        }
        return doc
    }

    _resolveDocId(docId) {
        const id = docId || this.options.defaultDocId
        if (
            id === this.options.defaultDocId &&
            this.activeDefaultDocId !== this.options.defaultDocId &&
            this.docs.has(this.activeDefaultDocId) &&
            !this.docs.has(this.options.defaultDocId)
        ) {
            return this.activeDefaultDocId
        }
        return id
    }

    _send(message) {
        if (!this.socket) return
        this.socket.send(JSON.stringify(message))
    }

    _setReadOnly(readonly) {
        for (const doc of this.docs.values()) {
            const editor = doc.binding?.editor
            if (!editor) continue
            if ('readOnly' in editor) editor.readOnly = readonly
            if ('readonly' in editor) editor.readonly = readonly
            if (editor.toggleAttribute) editor.toggleAttribute('readonly', readonly)
        }
    }

    _setStatus(status) {
        if (this.status === status) return
        this.status = status
        this._emit('status', status)
    }

    _resolveConnect(snapshot) {
        if (this._connectDeferred) {
            this._connectDeferred.resolve(snapshot)
            this._connectDeferred = null
        }
        this._lastConnectError = null
    }

    _emit(eventName, payload) {
        for (const handler of this.listeners.get(eventName) || []) {
            handler(payload)
        }
    }

    _readAnonToken() {
        try {
            if (this._anonTokenStorageKey) return this._anonTokenStorage?.getItem(this._anonTokenStorageKey)
        } catch {
            // Storage can be unavailable even after its property getter succeeds.
        }
        return null
    }

    _rememberAnonToken(token) {
        if (typeof token !== 'string' || !token) return
        this.anonToken = token
        try {
            if (this._anonTokenStorageKey) this._anonTokenStorage?.setItem(this._anonTokenStorageKey, token)
        } catch {
            // Quotas or browser policy must not prevent using the in-memory token.
        }
    }

    _httpBaseUrl() {
        return stripTrailingSlash(this.options.seanceUrl || this.options.serverUrl || '')
    }

    _wsUrl(sessionId) {
        const base = this._httpBaseUrl()
        const wsBase = base.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')
        return `${wsBase}/v1/sessions/${encodeURIComponent(sessionId)}/ws`
    }
}

function anonTokenStorage(options) {
    if (options.anonTokenStorage !== undefined) return options.anonTokenStorage || null
    try {
        return globalThis.sessionStorage || null
    } catch {
        return null
    }
}

function anonTokenStorageKey(base) {
    try {
        // Keep deployments on different paths, schemes or ports isolated. URL
        // normalizes host casing/default ports; the transport trims trailing '/'.
        const normalized = new URL(base || '/', globalThis.location?.href).href
        return `seance:anon-token:${stripTrailingSlash(normalized)}`
    } catch {
        return null
    }
}

function deferred() {
    let resolve
    let reject
    const promise = new Promise((res, rej) => {
        resolve = res
        reject = rej
    })
    return { promise, resolve, reject }
}

function clientError(code, detail, cause = null, frame = null) {
    const error = new Error(detail)
    error.type = 'error'
    error.code = code
    error.detail = detail
    if (cause) error.cause = cause
    if (frame) error.frame = frame
    return error
}

// Which text was the server holding when it built the snapshot: the one before
// or after our in-flight edit? Only exact matches provide a usable baseline.
// Otherwise a protocol receipt is required to resolve the ambiguity safely.
function resolveReconnectBase(oldServerText, inFlightEdit, snapshotText) {
    let applied
    try {
        applied = applyTextEdit(oldServerText, inFlightEdit)
    } catch {
        return null
    }
    if (applied === snapshotText) return applied
    if (oldServerText === snapshotText) return oldServerText
    return null
}

function chunkEditText(text) {
    const candidate = text.slice(0, MAX_EDIT_CHARS)
    const encoder = new TextEncoder()
    if (encoder.encode(JSON.stringify(candidate)).length <= MAX_EDIT_JSON_BYTES) return candidate
    let low = 0
    let high = candidate.length
    while (low < high) {
        const end = Math.ceil((low + high) / 2)
        if (encoder.encode(JSON.stringify(candidate.slice(0, end))).length <= MAX_EDIT_JSON_BYTES) low = end
        else high = end - 1
    }
    return candidate.slice(0, low)
}

function protocolError(msg) {
    const detail = msg?.detail || msg?.message || msg?.code || 'seance protocol error'
    const error = new Error(String(detail))
    error.code = msg?.code
    error.frame = msg
    return error
}

function addSocketListener(socket, type, handler) {
    if (socket.addEventListener) {
        socket.addEventListener(type, handler)
        return
    }
    const key = `on${type}`
    const previous = socket[key]
    socket[key] = (event) => {
        previous?.(event)
        handler(event)
    }
}

function normalizeBinding(binding, defaultDocId) {
    if (binding?.editor) {
        return { docId: binding.docId || defaultDocId, _usesDefaultDocId: !binding.docId, ...binding }
    }
    return { docId: defaultDocId, _usesDefaultDocId: true, editor: binding }
}

function validateBindingText(binding, text, context) {
    if (!binding?.validateText) return { ok: true }
    try {
        const result = binding.validateText(text, context)
        if (result === false) return { ok: false, reason: 'validation failed' }
        if (typeof result === 'string') return { ok: false, reason: result }
        if (result && typeof result === 'object' && result.ok === false) {
            return { ok: false, reason: result.reason || result.message || 'validation failed' }
        }
        return { ok: true }
    } catch (error) {
        return { ok: false, reason: error?.message || 'validation failed' }
    }
}

function notifyRemoteText(binding, text, context) {
    binding?.onRemoteText?.(text, context)
}

function notifyAcceptedText(binding, text, context) {
    binding?.onAcceptedText?.(text, context)
}

function readEditorText(binding) {
    if (binding.getText) return String(binding.getText())
    return String(binding.editor?.value ?? '')
}

function writeEditorText(binding, text) {
    if (binding.setText) {
        binding.setText(text, { source: 'remote' })
    } else if (binding.editor) {
        binding.editor.value = text
    }
}

function readSelection(binding) {
    const editor = binding?.editor
    if (!editor) return null
    if (editor.getSelectionRange) return editor.getSelectionRange()
    if (Number.isInteger(editor.selectionStart) && Number.isInteger(editor.selectionEnd)) {
        return {
            start: editor.selectionStart,
            end: editor.selectionEnd,
            direction: editor.selectionDirection || 'none',
        }
    }
    return null
}

function clearRemoteSelections(binding) {
    const editor = binding?.editor
    if (editor?.clearRemoteSelections) editor.clearRemoteSelections()
}

function detectHandfishFeatures(editor) {
    const version = Number(editor?.collabApiVersion || editor?.constructor?.collabApiVersion || 0)
    return {
        collabApiVersion: version,
        hasTextEdit: typeof editor?.applyTextEdit === 'function' || typeof editor?.replaceRange === 'function',
        hasRemoteSelection: typeof editor?.setRemoteSelection === 'function',
        hasSelectionApi: typeof editor?.getSelectionRange === 'function',
    }
}

function urlFrom(locationLike) {
    if (typeof locationLike === 'string') return new URL(locationLike, 'http://localhost/')
    if (locationLike instanceof URL) return new URL(locationLike.toString())
    if (locationLike?.href) return new URL(locationLike.href)
    return new URL('http://localhost/')
}

function stripTrailingSlash(value) {
    return String(value || '').replace(/\/+$/, '')
}
