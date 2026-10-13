'use strict';
// Pure kernel of the Extensions page: the card state follows the
// payload's busy_states and `effective`, an upgrade is told apart from an
// install, a job message's backup path is found, and the review card offers
// install / upgrade / nothing as the server decided. Also: Paused counts,
// Failed notes, the agent's word on a stuck row, quiet polls while the store
// restarts — the last three also rendered through the page class with a stub frappe.
// The version an upgrade puts in place, literal underscores in release notes, and a
// Staged card that names its backup once.
// Underscores left alone where markdown is not read; the failed-staging dialog follows the mode.
// Nothing rolls forward over a failed upgrade — the review card shows the refusal
// with its self-hosting-guide pointer, and an upgrade always takes a backup; a reference link's id is left as its
// definition spells it.
// A stuck row, dialogs and links on phones, Off cards; release notes keep mid-word underscores literal in
// the shared renderReleaseNotes (tests/js/release_notes.test.js), not in this page (showdown's own option).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const PAGE = path.join(__dirname, '../../ffl_core/page/extensions/extensions.js');
const {
	xtCardState, xtBusyKind, xtRestartBanner, xtPaused, xtSplitBackup, xtFailedNote, xtAgentFailure, xtQuietPoll, xtOutage,
	xtReviewMode, xtPermissionDiff, xtStagingSettled, xtTargetVersion, xtUpdateOffer,
} = require(PAGE);

const BUSY = ['Installing', 'Staged', 'Uninstalling', 'Upgrading'];
const row = (o) => Object.assign({ name: 'ext_hello', installed_on_site: 1, compatible: 1, enabled: 1, effective: 1, job_state: 'Idle' }, o);

test('busy comes from the payload list, not from the page', () => {
	assert.strictEqual(xtCardState(row({ job_state: 'Upgrading' }), BUSY), 'busy');
	assert.strictEqual(xtCardState(row({ job_state: 'Staged' }), ['Installing']), 'on');   // a state the server does not call busy
	assert.strictEqual(xtCardState(row({ job_state: 'Installing' }), undefined), 'on');
	assert.match(fs.readFileSync(PAGE, 'utf8'), /xtCardState\(row, this\.data\.busy_states\)/);
});

test('the switch follows effective; a paused extension keeps enabled but is not on', () => {
	assert.strictEqual(xtCardState(row({ installed_on_site: 0, effective: 0 }), BUSY), 'available');
	assert.strictEqual(xtCardState(row({ compatible: 0, effective: 0 }), BUSY), 'incompatible');
	assert.strictEqual(xtCardState(row({ enabled: 1, effective: 0 }), BUSY), 'off');
	assert.strictEqual(xtCardState(row({ enabled: 0, effective: 1 }), BUSY), 'on');
});

test('Staged is an upgrade only when the pending restart request is an upgrade of this id', () => {
	const staged = row({ job_state: 'Staged' });
	assert.strictEqual(xtBusyKind(staged, { action: 'upgrade', app: 'ext_hello' }), 'staged-upgrade');
	assert.strictEqual(xtBusyKind(staged, { action: 'install', app: 'ext_hello' }), 'staged');
	assert.strictEqual(xtBusyKind(staged, { action: 'upgrade', app: 'other' }), 'staged');
	assert.strictEqual(xtBusyKind(staged, null), 'staged');
	assert.deepStrictEqual(['Installing', 'Upgrading', 'Uninstalling', 'Mystery'].map((s) => xtBusyKind(row({ job_state: s }), null)),
		['installing', 'upgrading', 'uninstalling', 'other']);
});

test('restart banner: coming (install / upgrade), upgrading after the restart, silent otherwise', () => {
	assert.strictEqual(xtRestartBanner(null, [row()]), null);
	assert.strictEqual(xtRestartBanner({ action: 'install', app: 'new_ext' }, [row()]), 'install');                        // row not synced yet
	assert.strictEqual(xtRestartBanner({ action: 'upgrade', app: 'ext_hello' }, [row({ job_state: 'Staged' })]), 'upgrade');
	assert.strictEqual(xtRestartBanner({ action: 'upgrade', app: 'ext_hello' }, [row({ job_state: 'Upgrading' })]), 'upgrading');
	assert.strictEqual(xtRestartBanner({ action: 'install', app: 'ext_hello' }, [row({ job_state: 'Installing' })]), null);
});

// The backup lives outside private/backups and every message names it as "Backup: <absolute path>",
// followed only by a space or the end; the texts below are the backend's
const BACKUP = '/home/frappe/frappe-bench/sites/.ext-backups/pos.example.com/ext_hello/20260913-101500-database.sql.gz';
const HINT = `Backup: ${BACKUP} — docs/self-hosting.md#roll-back-an-upgrade.`;
const NO_BACKUP = "No database backup was recorded — docs/self-hosting.md#roll-back-an-upgrade.";

test('backup path (K3): "Backup: <absolute path>" in every message shape; anything else stays in the reason', () => {
	assert.deepStrictEqual(
		xtSplitBackup(`Upgrade failed while updating the database: column acme_x missing. ${HINT}`),
		{ reason: 'Upgrade failed while updating the database: column acme_x missing', backup: BACKUP });
	assert.deepStrictEqual(xtSplitBackup(`Putting ext_hello 0.2.0 in place. Backup: ${BACKUP}`), { reason: 'Putting ext_hello 0.2.0 in place', backup: BACKUP });
	assert.strictEqual(xtSplitBackup(`ext_hello 0.2.0 is on the server. Waiting for the store to restart; the POS is unavailable for a minute or two while the database is updated. Backup: ${BACKUP}`).backup, BACKUP);
	assert.deepStrictEqual(xtSplitBackup('Install failed: boom'), { reason: 'Install failed: boom', backup: '' });
	assert.deepStrictEqual(xtSplitBackup(`Upgrade failed while updating the database: x. ${NO_BACKUP}`), { reason: `Upgrade failed while updating the database: x. ${NO_BACKUP}`, backup: '' });
	assert.strictEqual(xtSplitBackup('Backup: ./dev.localhost/private/backups/x.sql.gz').backup, '');   // not the K3 shape
	assert.strictEqual(xtSplitBackup(`backup: ${BACKUP}`).backup, '');                                    // the token's case is fixed
	assert.deepStrictEqual(xtSplitBackup(null), { reason: '', backup: '' });
});

test('review card: install, upgrade, or blocked — as the server said', () => {
	assert.deepStrictEqual(xtReviewMode({ ok: true, upgrade: null, on_bench: false }), { mode: 'install', blocked: false });
	assert.deepStrictEqual(xtReviewMode({ ok: true, upgrade: null, on_bench: true }), { mode: 'install', blocked: true });
	assert.deepStrictEqual(xtReviewMode({ ok: false, upgrade: null }), { mode: 'install', blocked: true });
	const up = { from: '0.1.0', to: '0.2.0', allowed: true, reason: '' };
	assert.deepStrictEqual(xtReviewMode({ ok: true, on_bench: true, upgrade: up }), { mode: 'upgrade', blocked: false });
	assert.deepStrictEqual(xtReviewMode({ ok: false, on_bench: true, upgrade: up }), { mode: 'upgrade', blocked: true });
	assert.deepStrictEqual(xtReviewMode({ ok: true, on_bench: true, upgrade: { ...up, allowed: false, reason: 'same version' } }), { mode: 'upgrade', blocked: true });
});

test('permission diff: new ones carry high-risk, kept excludes them, removed as sent', () => {
	const d = xtPermissionDiff({
		permissions: ['desk_ui', 'network:api.example.com', 'scheduler'],
		high_risk: ['network:api.example.com'],
		upgrade: { permissions_added: ['network:api.example.com', 'scheduler'], permissions_removed: ['pos_ui'] },
	});
	assert.deepStrictEqual(d, {
		added: [{ perm: 'network:api.example.com', hi: true }, { perm: 'scheduler', hi: false }],
		kept: [{ perm: 'desk_ui', hi: false }],
		removed: ['pos_ui'],
	});
	assert.deepStrictEqual(xtPermissionDiff({ permissions: ['desk_ui'] }), { added: [], kept: [{ perm: 'desk_ui', hi: false }], removed: [] });
});

test('staging watch: an install settles when the row appears, an upgrade when its row moves', () => {
	assert.strictEqual(xtStagingSettled(undefined, null), false);
	assert.strictEqual(xtStagingSettled(row(), null), true);
	const before = { job_state: 'Idle', job_message: '', version: '0.1.0' };
	assert.strictEqual(xtStagingSettled(row({ job_message: '', version: '0.1.0' }), before), false);
	assert.strictEqual(xtStagingSettled(row({ job_state: 'Staged', job_message: '', version: '0.1.0' }), before), true);
	assert.strictEqual(xtStagingSettled(row({ job_message: 'Install failed: x', version: '0.1.0' }), before), true);
	assert.strictEqual(xtStagingSettled(row({ job_message: '', version: '0.2.0' }), before), true);
});

// --- Paused, Failed notes, quiet polls ---------------------------------------------------------------------------
// A stub desk for the page class: escape_html as frappe's, __ formats {n}.
globalThis.frappe = { utils: { escape_html: (t) => (t == null ? '' : String(t).replace(/[&<>"'`=]/g, (c) => `&#${c.charCodeAt(0)};`)) } };
globalThis.__ = (text, args) => String(text).replace(/\{(\d+)\}/g, (m, i) => (args || [])[i]);
const page = (props) => Object.assign(Object.create(gunstore.ExtensionsPage.prototype), { seenRequests: {} }, props);

test('Paused = installed, not compatible and still switched on; the status bar counts only those', () => {
	assert.strictEqual(xtPaused(row({ compatible: 0, enabled: 1 })), true);
	assert.strictEqual(xtPaused(row({ compatible: 0, enabled: 0 })), false);        // a locked Off
	assert.strictEqual(xtPaused(row({ compatible: 1, enabled: 1 })), false);
	assert.strictEqual(xtPaused(row({ compatible: 0, enabled: 1, installed_on_site: 0 })), false);
	let html = '';
	const p = page({
		data: { core_version: '0.1.0', busy: 0, incompatible: 2, restart: {}, extensions: [row({ name: 'a', compatible: 0, enabled: 1 }), row({ name: 'b', compatible: 0, enabled: 0 })] },
		$root: { find: () => ({ html: (h) => { html = h; }, on() {} }) },
	});
	p.renderStatus();
	assert.match(html, /1 paused — not compatible with this core/);
	assert.strictEqual(p.paused(row({ compatible: 0, enabled: 0 })), false);
});

test('Failed note: "nothing was changed" only when no backup was taken and it was not an upgrade', () => {
	assert.deepStrictEqual(xtFailedNote('Install failed: boom'), { reason: 'Install failed: boom', backup: '', unchanged: true });
	assert.strictEqual(xtFailedNote(`Upgrade failed while updating the database: x. ${HINT}`).unchanged, false);
	assert.strictEqual(xtFailedNote(`Upgrade failed before the database was updated: restart of backend failed. ${HINT}`).unchanged, false);
	assert.strictEqual(xtFailedNote('Upgrade failed: the replay broke. The previous version is still in place.').unchanged, false);
	assert.strictEqual(xtFailedNote(`Upgrade failed while updating the database: x. ${NO_BACKUP}`).unchanged, false);
	assert.strictEqual(xtFailedNote(`The upgrade did not finish within 30 minutes (the database update may have stopped). ${NO_BACKUP}`).unchanged, false);
	// A row that failed before the guide moved: old pointer, no "Upgrade failed" prefix, no backup.
	assert.strictEqual(xtFailedNote("The upgrade did not finish within 30 minutes (the database update may have stopped). No database backup was recorded — RUNBOOK §14 'Roll back an upgrade'.").unchanged, false);
	assert.strictEqual(xtFailedNote('Restart failed. See RUNBOOK §14.').unchanged, false);

	const note = (message) => page().failedNote(row({ job_state: 'Failed', job_message: message }));
	const install = note('Install failed: boom');
	assert.match(install, /Nothing on this POS was changed\./);
	assert.doesNotMatch(install, /xt-backup/);

	const double = note(`Upgrade failed: ${'x'.repeat(250)} … ERROR: bad bundle. Putting the previous version back failed too — see Error Log. ${HINT}`);
	assert.doesNotMatch(double, /Nothing on this POS was changed/);
	assert.match(double, /Putting the previous version back failed too/);                  // the key sentence is not cut at 160
	assert.match(double, /give this path to your developer to roll back/);
	assert.ok(double.includes(`data-path="${BACKUP}"`), double);

	const restored = note('Upgrade failed: the replay broke. The previous version is still in place.');
	assert.doesNotMatch(restored, /Nothing on this POS was changed|xt-backup/);
	assert.match(restored, /The previous version is still in place\. You can try again/);
});

test("the agent's word on a stuck row: only a failed done for this row's request, after it was seen pending (or while Upgrading)", () => {
	const seen = { action: 'install', app: 'ext_hello', requested_at: '2026-09-13T10:00:00+00:00' };
	const done = (o) => ({ pending: null, done: Object.assign({ app: 'ext_hello', ok: false, message: 'restart of backend failed', finished_at: '2026-09-13T10:00:40+00:00' }, o) });
	const staged = row({ job_state: 'Staged', job_message: 'Code is on the server.' });
	assert.deepStrictEqual(xtAgentFailure(staged, done(), BUSY, seen), { reason: 'restart of backend failed', backup: '', upgrade: false });
	assert.strictEqual(xtAgentFailure(staged, done(), BUSY, undefined), null);                                      // never saw its request: maybe a job that has not asked yet
	assert.strictEqual(xtAgentFailure(staged, done({ finished_at: '2026-09-13T09:59:59+00:00' }), BUSY, seen), null); // an earlier attempt's done
	assert.strictEqual(xtAgentFailure(staged, { pending: seen, done: done().done }, BUSY, seen), null);                // still pending
	assert.strictEqual(xtAgentFailure(staged, done({ ok: true }), BUSY, seen), null);
	assert.strictEqual(xtAgentFailure(staged, done({ app: 'other' }), BUSY, seen), null);
	assert.strictEqual(xtAgentFailure(row({ job_state: 'Failed' }), done(), BUSY, seen), null);                        // the row is not busy: its own note says it
	const upgrading = row({ job_state: 'Upgrading', job_message: 'Updating the database for ext_hello 0.2.0.' });
	assert.deepStrictEqual(xtAgentFailure(upgrading, done({ message: 'finish_upgrade failed (bench execute exit 1; migrate failed): boom' }), BUSY, undefined),
		{ reason: 'finish_upgrade failed (bench execute exit 1; migrate failed): boom', backup: '', upgrade: true });
	const stagedUp = row({ job_state: 'Staged', job_message: `ext_hello 0.2.0 is on the server. Waiting for the store to restart; the POS is unavailable for a minute or two while the database is updated. Backup: ${BACKUP}` });
	assert.deepStrictEqual(xtAgentFailure(stagedUp, done({ message: 'backend never finished its extension replay (600 s)' }), BUSY, { ...seen, action: 'upgrade' }),
		{ reason: 'backend never finished its extension replay (600 s)', backup: BACKUP, upgrade: true });

	// the page remembers a request while it is pending and forgets it once the row is no longer busy
	const p = page({ renderStatus() {}, renderTabs() {}, renderList() {}, renderFoot() {}, startPolling() {}, stopPolling() {}, $root: { find: () => ({ prop() {}, children: () => [1] }) } });
	p.render({ busy_states: BUSY, busy: 1, restart: { pending: seen }, extensions: [staged] });
	assert.deepStrictEqual(p.seenRequests, { ext_hello: seen });
	p.render({ busy_states: BUSY, busy: 1, restart: done(), extensions: [staged] });
	assert.deepStrictEqual(p.seenRequests, { ext_hello: seen });
	const html = p.stuckNote(xtAgentFailure(staged, done(), BUSY, p.seenRequests.ext_hello));
	assert.match(html, /The store could not finish this\.<\/b> restart of backend failed\. Ask your developer to check the ext-agent timer/);
	assert.match(p.pills(staged, 'busy', xtAgentFailure(staged, done(), BUSY, seen)), /Did not finish/);
	assert.match(p.stuckNote({ reason: 'backend never finished', backup: BACKUP, upgrade: true }), /roll back[^]*xt-backup/);
	p.render({ busy_states: BUSY, busy: 0, restart: done(), extensions: [row({ job_state: 'Failed' })] });
	assert.deepStrictEqual(p.seenRequests, {});
});

test('a poll while the store restarts (pending request or busy row) is quiet on 502/503/504 or no answer, and never stacks', async () => {
	assert.strictEqual(xtQuietPoll({ busy: 1, restart: {} }), true);
	assert.strictEqual(xtQuietPoll({ busy: 0, restart: { pending: { app: 'x' } } }), true);
	assert.strictEqual(xtQuietPoll({ busy: 0, restart: { pending: null } }), false);
	assert.deepStrictEqual([200, 403, 500, 502, 503, 504].map(xtOutage), [false, false, false, true, true, true]);

	let loads = 0; const renders = [];
	const p = page({ data: { busy: 1, restart: { pending: null } }, load: () => { loads += 1; return Promise.resolve(); }, render: (d) => renders.push(d) });
	for (const status of [502, 503, 504]) {
		globalThis.fetch = async () => ({ status, ok: false });
		await p.poll();
	}
	globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
	await p.poll();
	assert.deepStrictEqual([loads, renders], [0, []]);                        // nothing shown, nothing rendered
	globalThis.fetch = async () => ({ status: 200, ok: true, json: async () => ({ message: { busy: 0 } }) });
	await p.poll();
	assert.deepStrictEqual(renders, [{ busy: 0 }]);
	globalThis.fetch = async () => ({ status: 403, ok: false });
	await p.poll();
	assert.strictEqual(loads, 1);                                             // a real error goes through frappe, which shows it

	let calls = 0; let answer;
	globalThis.fetch = () => { calls += 1; return new Promise((resolve) => { answer = resolve; }); };
	const first = p.poll(); p.poll(); p.poll();
	assert.strictEqual(calls, 1);
	answer({ status: 502, ok: false });
	await first;
	p.data = { busy: 0, restart: { pending: null } };
	calls = 0;
	await p.poll();
	assert.deepStrictEqual([calls, loads], [0, 2]);                           // an idle page polls through frappe as before
});

test('the 3 s timer runs poll(), not a bare load()', (t) => {
	t.mock.timers.enable({ apis: ['setInterval'] });
	let polls = 0; let loads = 0;
	const p = page({ poll: () => { polls += 1; }, load: () => { loads += 1; } });
	p.startPolling();
	t.mock.timers.tick(9000);
	p.stopPolling();
	assert.deepStrictEqual([polls, loads], [3, 0]);
});

// --- Upgrade version, release notes, Staged card ---------------------------------------------------------------------------
const STAGED_UP = `ext_hello 0.2.0 is on the server. Waiting for the store to restart; the POS is unavailable for a minute or two while the database is updated. Backup: ${BACKUP}`;
const UPGRADING = `Updating the database for ext_hello 0.2.0. The POS is unavailable until this finishes. Backup: ${BACKUP}`;

test('progress copy names the version being put in place (the job message), not the row version, still the old one until finish', () => {
	assert.strictEqual(xtTargetVersion(row({ job_message: STAGED_UP })), '0.2.0');
	assert.strictEqual(xtTargetVersion(row({ job_message: `Putting ext_hello 0.3.0rc1 in place. Backup: ${BACKUP}` })), '0.3.0rc1');
	assert.strictEqual(xtTargetVersion(row({ job_message: UPGRADING.replace('0.2.0', '1.0.0-beta.2') })), '1.0.0-beta.2');
	assert.strictEqual(xtTargetVersion(row({ job_message: 'Code is on the server. Waiting for the store to restart.' })), '');
	assert.strictEqual(xtTargetVersion(row({ job_message: 'ext_hello_dep 2.0 is bundled' })), '');           // a longer word is not the id
	assert.strictEqual(xtTargetVersion(row({ job_message: null })), '');

	const pending = { action: 'upgrade', app: 'ext_hello' };
	const staged = page({ data: { restart: { pending } } }).busyCopy(row({ version: '0.1.0', job_state: 'Staged', job_message: STAGED_UP }));
	const upgrading = page({ data: { restart: {} } }).busyCopy(row({ version: '0.1.0', job_state: 'Upgrading', job_message: UPGRADING }));
	assert.match(staged.text, /^Version 0\.2\.0 is on the server\./);
	assert.match(upgrading.text, /^Updating the database for version 0\.2\.0\./);
	assert.doesNotMatch(staged.text + upgrading.text, /0\.1\.0/);
	const unnamed = page({ data: { restart: { pending } } }).busyCopy(row({ version: '0.1.0', job_state: 'Staged', job_message: '' }));
	assert.match(unnamed.text, /^The new version is on the server\./);
	assert.doesNotMatch(page({ data: { restart: {} } }).busyCopy(row({ version: '0.1.0', job_state: 'Upgrading', job_message: '' })).text, /0\.1\.0/);
});

test('a Staged card without a pending request names its backup once: the progress line drops "Backup: …", the Backup row keeps it', () => {
	const p = page({ data: { restart: { pending: null } } });
	for (const message of [`Putting ext_hello 0.2.0 in place. Backup: ${BACKUP}`, STAGED_UP]) {
		const text = p.busyCopy(row({ job_state: 'Staged', job_message: message })).text;
		assert.ok(!text.includes('Backup') && !text.includes(BACKUP), text);
		assert.ok(message.startsWith(text.trim().replace(/\.$/, '')), text);
	}
	assert.strictEqual(p.busyCopy(row({ job_state: 'Staged', job_message: 'Code is on the server. Waiting for the store to restart.' })).text.trim(),
		'Code is on the server. Waiting for the store to restart.');
	assert.strictEqual(p.backupRow(xtSplitBackup(STAGED_UP).backup).split(BACKUP).length - 1, 2);      // <code> and data-path: one row
});

test('an Upgrading card that did not finish names its backup: begin_upgrade writes it into the message (K3)', () => {
	const done = { pending: null, done: { app: 'ext_hello', ok: false, message: 'finish_upgrade failed (bench execute exit 1; migrate failed): boom', finished_at: '2026-09-13T10:00:40+00:00' } };
	const f = xtAgentFailure(row({ job_state: 'Upgrading', job_message: UPGRADING }), done, BUSY, undefined);
	assert.deepStrictEqual(f, { reason: 'finish_upgrade failed (bench execute exit 1; migrate failed): boom', backup: BACKUP, upgrade: true });
	const html = page().stuckNote(f);
	assert.match(html, /give this path to your developer to roll back/);
	assert.ok(html.includes(`data-path="${BACKUP}"`), html);
});

test('a staged upload that failed on the server: the dialog says upgraded or installed, as the Install / Upgrade click was', async () => {
	const titles = [];
	globalThis.frappe.msgprint = (d) => titles.push(d.title);
	globalThis.frappe.xcall = async () => ({ app: 'ext_hello', message: 'The store is busy: ext_other is Staged.' });
	for (const mode of ['upgrade', 'install', undefined]) {
		const p = page({ stagingWatch: { app_id: 'ext_hello', sha256: 'x', title: 'Hello', mode, since: Date.now() - 5000, before: { job_state: 'Idle', job_message: '', version: '0.1.0' } } });
		p.checkStaging({ extensions: [row({ job_message: '', version: '0.1.0' })] });
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.deepStrictEqual(titles, ['Hello could not be upgraded', 'Hello could not be installed', 'Hello could not be installed']);
});

test('release notes: both places the page renders them hand the notes as written to the shared renderReleaseNotes, which keeps their underscores literal', async () => {
	const rendered = [];
	globalThis.window = globalThis;
	gunstore.renderReleaseNotes = (entries) => { rendered.push(entries); return '<p>notes</p>'; };
	globalThis.DOMParser = class { parseFromString(html) { return { querySelectorAll: () => [], body: { innerHTML: html } }; } };
	page().notesFold({ from: '0.1.0', to: '0.2.0', release_notes: 'ext_hello_dep' });
	globalThis.frappe.xcall = async () => [['0.2.0', 'uses ext_hello_dep']];
	globalThis.frappe.msgprint = () => {};
	await page().releaseNotes(row({ title: 'Hello' }));
	assert.deepStrictEqual(rendered, [[['0.2.0', 'ext_hello_dep']], [['0.2.0', 'uses ext_hello_dep']]]);
	assert.doesNotMatch(fs.readFileSync(PAGE, 'utf8'), /xtLiteralUnderscores/, 'no second escape on the page');
});

// --- Failed upgrades, review-card refusals --------------------------------------------------------------------
test('an upload over an upgrade that did not finish: Cannot upgrade, the reason with its self-hosting-guide pointer, nothing about keeping a rollback point', () => {
	const reason = "The upgrade of ext_hello to 0.2.0 did not finish. Ask your developer to roll it back (docs/self-hosting.md#roll-back-an-upgrade) before uploading again.";
	let html = '';
	const $r = { empty: () => $r, html: (h) => { html = h; return $r; }, find: () => ({ on() {}, prop() {}, val: () => '' }) };
	globalThis.frappe.form = { formatters: {} };
	const p = page({ $root: { find: () => $r } });
	const report = (upgrade) => ({
		ok: true, app_id: 'ext_hello', title: 'Hello', version: upgrade.to, sha256: 'x', size: 1, findings: [], permissions: [], high_risk: [], on_bench: true,
		upgrade: { permissions_added: [], permissions_removed: [], release_notes: '', ...upgrade },
	});
	p.renderReview(report({ from: '0.2.0', to: '0.3.0', allowed: false, reason }));
	assert.match(html, /<span class="xt-pill bad">Cannot upgrade<\/span>/);
	assert.ok(html.includes(`This package cannot replace the version on this server: ${frappe.utils.escape_html(reason)}`), html);
	assert.match(html, /Ask your developer to roll it back \(docs\/self-hosting\.md#roll-back-an-upgrade\)/);
	assert.match(html, /id="xt-confirm-id" class="form-control" autocomplete="off" disabled>/);
	assert.doesNotMatch(html, /No new backup|way back is still|kept when the upgrade/i);
	// A refused package changes nothing — no "What happens" promising a backup (or a copy, for an install)
	assert.doesNotMatch(html, /What happens|backup is taken/);
	p.renderReview({ ...report({ from: '0.1.0', to: '0.2.0', allowed: true, reason: '' }), ok: false, findings: [{ level: 'error', rule: 'hooks', message: 'not allowed' }] });
	assert.doesNotMatch(html, /What happens|backup is taken/);
	p.renderReview({ ...report({}), upgrade: null, on_bench: false, ok: false, findings: [{ level: 'error', rule: 'hooks', message: 'not allowed' }] });
	assert.doesNotMatch(html, /What happens|code is copied/);

	p.renderReview(report({ from: '0.1.0', to: '0.2.0', allowed: true, reason: '' }));
	assert.match(html, /What happens<\/span><span class="v">A database backup is taken, version 0\.2\.0 replaces 0\.1\.0 on the server/);
	let opts;
	globalThis.frappe.ui = { Dialog: class { constructor(o) { opts = o; this.$wrapper = { addClass() {} }; } show() {} } };
	p.confirmUpgrade({ title: 'Hello', app_id: 'ext_hello', upgrade: { from: '0.1.0', to: '0.2.0', allowed: true } }, 'ext_hello');
	assert.match(opts.fields[0].options, /First<\/span><span class="v">A database backup is taken automatically\.<\/span>/);
	assert.strictEqual(opts.primary_action_label, 'Back up and upgrade');
	assert.doesNotMatch(fs.readFileSync(PAGE, 'utf8'), /rollback_point|rollbackLine|upgradeSteps|No new backup/);
});


// --- Stuck rows, phones, Off cards ---------------------------------------------------------------------------
test('a row the store could not finish shows one state, "Did not finish" — no disabled "Working…" button beside it', () => {
	const built = []; const appended = [];
	const $el = { find: () => $el, on: () => $el, append: (h) => { appended.push(h); return $el; } };
	globalThis.$ = (h) => { built.push(h); return $el; };
	const seen = { action: 'install', app: 'ext_hello', requested_at: '2026-09-13T10:00:00+00:00' };
	const done = { app: 'ext_hello', ok: false, message: 'restart of backend failed', finished_at: '2026-09-13T10:00:40+00:00' };
	const staged = row({ job_state: 'Staged', job_message: 'Code is on the server.' });
	page({ data: { busy_states: BUSY, restart: { pending: null, done }, extensions: [staged] }, seenRequests: { ext_hello: seen } }).card(staged);
	assert.match(built[0], /Did not finish/);
	assert.deepStrictEqual(appended.filter((h) => /Working…/.test(h)), []);
	built.length = 0;
	page({ data: { busy_states: BUSY, restart: { pending: seen }, extensions: [staged] } }).card(staged);        // still waiting: busy as before
	assert.doesNotMatch(built[0], /Did not finish/);
	assert.strictEqual(appended.filter((h) => /Working…/.test(h) && /disabled/.test(h)).length, 1);
});

test('≤768px: the turn-off and uninstall dialogs get 44px footer buttons (xt-dialog), the card and status-bar text links a 44px tap height', async (t) => {
	t.mock.timers.enable({ apis: ['setInterval'] });
	const dialogs = [];
	globalThis.frappe.ui = { Dialog: class {
		constructor(o) { this.o = o; this.classes = []; dialogs.push(this); this.$wrapper = { addClass: (c) => { this.classes.push(c); return this.$wrapper; }, on() {} }; }
		show() {} get_value() { return ''; }
		get_primary_btn() { const b = { addClass: () => b, removeClass: () => b, prop: () => b }; return b; }
	} };
	const answers = { 'ffl_core.api.extensions.disable_notice': 'Layaway plans stop reminding customers.' };
	globalThis.frappe.xcall = async (method) => answers[method];
	const p = page({ load() {} });
	await p.toggle(row({ title: 'Hello' }), false);
	for (const preview of [{ blocked: true, records: 3, doctypes: [{ name: 'Plan', rows: 3 }] }, { blocked: false, doctypes: [], roles: [] }]) {
		answers['ffl_core.api.extensions.uninstall_preview'] = preview;
		p.uninstall(row({ title: 'Hello' }));
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.deepStrictEqual(dialogs.map((d) => [d.o.title, d.classes.includes('xt-dialog')]),
		[['Turn off Hello?', true], ['Hello has records', true], ['Uninstall Hello?', true]]);
	const mobile = gunstore.ExtensionsPage.css().split('@media (max-width:768px)')[1];
	assert.match(mobile, /\.xt-dialog \.modal-footer \.btn[^{]*\{min-height:44px/);
	assert.match(mobile, /\.xt-dialog \.modal-header \.btn-modal-close[^{]*\{min-height:44px;min-width:44px/, 'the close button (28px in frappe) too');
	assert.match(mobile, /(^|[\s,])\.xt-link[^{]*\{[^}]*min-height:44px/);
});

test('an Off card (and a locked one) fades only its decorative icon: opacity on text drops the light theme under 4.5:1 (#525252 at .68 on white ≈ 3.5:1)', () => {
	const css = gunstore.ExtensionsPage.css();
	const faded = [...css.matchAll(/([^{}]+)\{([^{}]*\bopacity\s*:[^{}]*)\}/g)]
		.flatMap(([, selectors]) => selectors.split(/,(?![^(]*\))/).map((s) => s.trim()))
		.filter((s) => /\.(off|incompatible)\b/.test(s) && !/\.xt-(pill|switch)/.test(s));
	assert.ok(faded.length, 'an Off card still looks dimmer');
	for (const s of faded) assert.match(s, /\s\.xt-icon$/, `${s}: fades text`);
});

// --- Failed upgrade after a migrate --------------------------------------------------------------------
test("the 30-minute sweep's Failed upgrade card: a migrate that got through is finished, so no way back through the backup is offered; any other keeps it", () => {
	// registry.reset_stale_jobs' texts for a Staged upgrade row, by the upgrade record's migrate
	const swept = (note, ask) => `The store did not restart within 30 minutes, so the upgrade never finished. ${note} Ask your developer to ${ask}. ${HINT}`;
	const note = (message) => page().failedNote(row({ job_state: 'Failed', job_message: message }));
	const finish = note(swept('The database was updated; only finishing the upgrade was not recorded.', 'finish it'));
	assert.match(finish, /only finishing the upgrade was not recorded\. Ask your developer to finish it\. <div class="xt-backup">/);
	assert.doesNotMatch(finish, /roll back|You can try again/, 'a restore would lose what was written since the backup');
	assert.ok(finish.includes(`data-path="${BACKUP}"`), 'the backup row stays: finishing by hand can fail and send the developer to the rollback');
	for (const [migrate, text] of [['not_started', 'The database was not updated.'], ['started', 'The database update may be half done.']]) {
		const back = note(swept(text, 'roll it back'));
		assert.match(back, /Ask your developer to roll it back\. A backup was taken before it started — give this path to your developer to roll back/, migrate);
		assert.ok(back.includes(`data-path="${BACKUP}"`), migrate);
	}
});

test("every Failed upgrade card whose migrate got through says finish it and offers no way back through the backup: the Upgrading sweep and finish_upgrade failing after the migrate", () => {
	const note = (message) => page().failedNote(row({ job_state: 'Failed', job_message: message }));
	for (const message of [
		`The upgrade did not finish within 30 minutes. The database was updated; only finishing the upgrade was not recorded. Ask your developer to finish it. ${HINT}`,
		`Upgrade failed after the database was updated (nothing to roll back; finishing it was not recorded): db went away. Ask your developer to finish it. ${HINT}`,
	]) {
		const html = note(message);
		assert.match(html, /Ask your developer to finish it\. <div class="xt-backup">/, message);
		assert.doesNotMatch(html, /give this path to your developer to roll back|You can try again/, message);
	}
	const back = note(`The upgrade did not finish within 30 minutes. The database update may be half done. Ask your developer to roll it back. ${HINT}`);
	assert.match(back, /give this path to your developer to roll back/);
});

test("the agent's give-up after a migrate that got through says finish it, not roll back; any other upgrade give-up keeps the way back", () => {
	const agent = fs.readFileSync(path.join(__dirname, '../../../../../deploy/prod/ext-agent.sh'), 'utf8');
	const outcome = agent.match(/outcome="(succeeded[^"]*)"/)[1];              // the words ext-agent.sh writes into the done file
	const note = (reason) => page().stuckNote({ reason, backup: BACKUP, upgrade: true });
	const finish = note(`finish_upgrade failed (bench execute exit 1; migrate ${outcome}): ImportError`);
	assert.match(finish, /The database was updated — ask your developer to finish it/);
	assert.doesNotMatch(finish, /give this path to your developer to roll back|Ask your developer to roll it back/);
	assert.ok(finish.includes(`data-path="${BACKUP}"`), 'the backup row stays');
	assert.match(note('finish_upgrade failed (bench execute exit 1; migrate failed): boom'), /give this path to your developer to roll back/);
	assert.match(page().stuckNote({ reason: 'backend never finished', backup: '', upgrade: true }), /Ask your developer to roll it back/);
});

test('the update service offer: a pill, and a button only when this POS can run it and nothing is busy', () => {
	const offered = (o) => row({ update: Object.assign({ version: '1.3.1', compatible: true, reason: '' }, o) });
	assert.deepStrictEqual(xtUpdateOffer(offered(), 'on'), { version: '1.3.1', compatible: true, button: true });
	assert.deepStrictEqual(xtUpdateOffer(offered(), 'off'), { version: '1.3.1', compatible: true, button: true });
	assert.deepStrictEqual(xtUpdateOffer(offered(), 'incompatible'), { version: '1.3.1', compatible: true, button: true },
		'a newer version is how a paused extension catches up with the core');
	assert.strictEqual(xtUpdateOffer(offered(), 'busy').button, false);
	assert.deepStrictEqual(xtUpdateOffer(offered({ compatible: false }), 'on'), { version: '1.3.1', compatible: false, button: false });
	assert.strictEqual(xtUpdateOffer(offered(), 'available'), null, 'not installed here: Install, not Update');
	assert.strictEqual(xtUpdateOffer(row({ update: null }), 'on'), null);
	const page = fs.readFileSync(PAGE, 'utf8');
	assert.match(page, /__\('\{0\} available', \[e\(offer\.version\)\]\)/, 'the version is escaped');
	assert.match(page, /this\.confirmUpgrade\(r, r\.app_id, \{ fromUpdate: true \}\)/, 'the page sends the id after its own confirm');
});
