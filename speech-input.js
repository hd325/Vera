/* Dictation owns only the selected range of one composer, for one recording. */
(function (global) {
    'use strict';

    function joinText(left, right) {
        if (!left || !right) return left + right;
        // Latin words need a boundary; Chinese, punctuation and existing spaces do not.
        const space = /[\p{L}\p{N}]$/u.test(left) && /^[\p{L}\p{N}]/u.test(right) &&
            !/\p{Script=Han}$/u.test(left) && !/^\p{Script=Han}/u.test(right);
        return left + (space ? ' ' : '') + right;
    }

    function create(options) {
        const bindings = options.bindings.filter(item => item.button && item.input);
        let active = null;
        let pending = null;
        let revision = 0;

        function updateButtons() {
            for (const { button } of bindings) {
                const recording = active?.button === button;
                const polishing = pending?.button === button;
                button.classList.toggle('recording', recording);
                button.setAttribute('aria-pressed', String(recording));
                button.setAttribute('aria-busy', String(polishing));
                const label = recording ? '停止語音輸入' : polishing ? '正在整理語音，可繼續編輯' : '語音輸入';
                button.title = label;
                button.setAttribute('aria-label', label);
            }
        }

        function cancelPolish() {
            if (pending) pending.controller.abort();
            pending = null;
        }

        function clearTimers(run) {
            clearTimeout(run.durationTimer);
            clearTimeout(run.drainTimer);
        }

        function cancel() {
            ++revision;
            cancelPolish();
            const run = active;
            active = null; // Invalidate before abort(), which may dispatch callbacks.
            if (run) {
                clearTimers(run);
                try { run.recognition.abort(); } catch (_) {}
            }
            updateButtons();
        }

        function replaceVoice(run, text) {
            const { input } = run;
            const value = joinText(run.before, text);
            const next = joinText(value, run.after);
            // A script or native editor may change .value without emitting input.
            if (input.value !== run.expected) { cancel(); return false; }
            const start = input.selectionStart;
            const end = input.selectionEnd;
            const previousEnd = run.voiceEnd ?? run.start;
            const delta = value.length - previousEnd;
            input.value = next;
            run.expected = next;
            run.voiceEnd = value.length;
            const move = position => position < run.start ? position :
                position >= previousEnd ? position + delta : value.length;
            input.setSelectionRange(move(start), move(end));
            options.onChange?.(input);
            return true;
        }

        async function polish(run) {
            if (!options.polish || !run.text?.trim() || run.input.value !== run.expected) return;
            const request = { ...run, run, controller: new AbortController() };
            pending = request;
            updateButtons();
            try {
                const text = await options.polish(run.text, request.controller.signal);
                if (pending !== request || request.controller.signal.aborted || active ||
                    typeof text !== 'string' || !text.trim() || run.input.value !== run.expected) return;
                // Only the dictated range is replaced; typed prefix/suffix stay intact.
                replaceVoice(run, text.trim());
            } catch (_) { /* Keep the visible transcript when polishing is unavailable. */ }
            finally {
                if (pending === request) { pending = null; updateButtons(); }
            }
        }

        function finish(run) {
            if (active !== run) return;
            clearTimers(run);
            active = null;
            updateButtons();
            run.polishPromise = polish(run);
        }

        async function prepareForSend(input) {
            const run = active?.input === input ? active : pending?.input === input ? pending.run : null;
            if (!run) return true;
            const requestedRevision = revision;
            if (active === run) {
                clearTimers(run);
                active = null;
                try { run.recognition.abort(); } catch (_) {}
                updateButtons();
                // Submit the visible draft; a delayed recognition result cannot change it.
                run.polishPromise = polish(run);
            }
            await run.polishPromise;
            return revision === requestedRevision && input.value === run.expected;
        }

        function stop() {
            const run = active;
            if (!run || run.state === 'stopping') return;
            run.state = 'stopping';
            clearTimeout(run.durationTimer);
            // stop() may deliver a final result before onend; keep this run valid briefly.
            run.drainTimer = setTimeout(() => {
                if (active !== run) return;
                finish(run);
                try { run.recognition.abort(); } catch (_) {}
            }, options.drainTimeout ?? 1500);
            try { run.recognition.stop(); }
            catch (_) { finish(run); }
        }

        function start(binding) {
            cancel();
            if (!options.SpeechRecognition) { options.onError?.('unsupported'); return; }
            const { input, button } = binding;
            const start = input.selectionStart ?? input.value.length;
            const end = input.selectionEnd ?? start;
            let recognition;
            try { recognition = new options.SpeechRecognition(); }
            catch (_) { options.onError?.('start-failed'); return; }
            const run = {
                recognition, input, button, start, voiceEnd: end, before: input.value.slice(0, start),
                after: input.value.slice(end), expected: input.value, finals: new Map(), text: '', state: 'starting'
            };
            active = run; // Guard rapid clicks before the asynchronous onstart.
            recognition.continuous = true;
            recognition.interimResults = true;
            recognition.lang = 'zh-TW';
            recognition.maxAlternatives = 1;
            recognition.onstart = () => {
                if (active !== run) { try { recognition.abort(); } catch (_) {} return; }
                if (run.state !== 'starting') return;
                run.state = 'listening';
            };
            recognition.onresult = event => {
                if (active !== run) return;
                if (input.value !== run.expected) { cancel(); return; }
                const segments = new Map(run.finals);
                // results is a session snapshot. A final result index is committed once.
                for (let i = 0; i < event.results.length; i++) {
                    if (run.finals.has(i)) continue;
                    const result = event.results[i];
                    const text = (result?.[0]?.transcript || '').trim();
                    segments.set(i, text);
                    if (result.isFinal) run.finals.set(i, text);
                }
                run.text = [...segments].sort((a, b) => a[0] - b[0])
                    .reduce((text, [, segment]) => joinText(text, segment), '');
                if (run.text || run.rendered) {
                    run.rendered = true;
                    replaceVoice(run, run.text);
                }
            };
            recognition.onend = () => finish(run);
            recognition.onerror = event => {
                if (active !== run) return;
                cancel();
                if (!['no-speech', 'aborted'].includes(event.error)) options.onError?.(event.error);
            };
            updateButtons();
            run.durationTimer = setTimeout(stop, options.maxDuration ?? 600000);
            try { recognition.start(); }
            catch (_) {
                if (active === run) cancel();
                options.onError?.('start-failed');
            }
        }

        for (const binding of bindings) {
            binding.button.addEventListener('click', () => {
                if (active?.button === binding.button && active.state !== 'stopping') stop();
                else start(binding);
            });
            for (const event of ['beforeinput', 'input', 'compositionstart']) {
                binding.input.addEventListener(event, () => {
                    if (active?.input === binding.input || pending?.input === binding.input) cancel();
                });
            }
        }
        updateButtons();
        return { cancel, stop, prepareForSend };
    }

    global.HoukongSpeechInput = { create };
})(window);
