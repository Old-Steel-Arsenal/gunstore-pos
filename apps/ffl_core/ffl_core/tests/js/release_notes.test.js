'use strict';
// Release notes → HTML, the one function What's New and the Extensions page share:
// window.gunstore.renderReleaseNotes in public/js/ffl_core.bundle.js. frappe.markdown is frappe's one shared showdown
// converter (frappe.md2html, made on first use); by default it reads an underscore inside a word as emphasis —
// ext_hello_dep became ext<em>hello</em>dep. The function turns showdown's own literalMidWordUnderscores on for its
// call only. The bundle is desk code that does not load under Node: this runs its
// releaseNotes block alone, with a frappe.markdown built the way frappe 16's utils/tools.js builds it (its HTML
// sanitizer, which keeps a's href and title, left out).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const BUNDLE = fs.readFileSync(path.join(__dirname, '../../public/js/ffl_core.bundle.js'), 'utf8');
const ROOT = path.resolve(__dirname, '../../../../..');
const CHANGE_LOG = path.join(__dirname, '../../change_log');
// Every note of every major line (change_log/v0/, later v1/ …), as frappe reads them.
const NOTES = fs.readdirSync(CHANGE_LOG).filter((d) => /^v\d+$/.test(d))
	.flatMap((d) => fs.readdirSync(path.join(CHANGE_LOG, d)).map((f) => path.join(d, f)));

// frappe's own showdown: a worktree's bench (frappe-bench/ inside the checkout) or CI's (next to it). CI's JS step runs
// before the bench is installed, so there the cases that render with it are skipped and the recording converter below
// is what runs.
const SHOWDOWN = [path.join(ROOT, 'frappe-bench'), path.join(ROOT, '..', 'frappe-bench')]
	.map((bench) => path.join(bench, 'apps/frappe/node_modules/showdown'))
	.find((dir) => fs.existsSync(path.join(dir, 'package.json')));
const needsShowdown = { skip: !SHOWDOWN && 'frappe-bench/apps/frappe/node_modules/showdown not found (CI runs JS tests before the bench)' };

// A converter with showdown's option API that records the options each makeHtml saw.
class RecordingConverter {
	constructor(options) { this.options = { ...options }; this.calls = []; }
	getOption(key) { return this.options[key]; }
	setOption(key, value) { this.options[key] = value; }
	makeHtml(text) {
		this.calls.push({ text, literalMidWordUnderscores: this.options.literalMidWordUnderscores });
		if (text === 'boom') throw new Error('boom');
		return `<p>${text}</p>`;
	}
}

function load(Converter) {
	const start = BUNDLE.indexOf('(function releaseNotes() {');
	assert.ok(start >= 0, 'the bundle has its releaseNotes block');
	const frappe = {
		markdown(txt) {                                            // frappe 16 utils/tools.js: made on first use, then shared
			if (!frappe.md2html) frappe.md2html = new Converter({ tables: true });
			return frappe.md2html.makeHtml(txt);
		},
	};
	const sandbox = { window: {}, frappe };
	vm.runInNewContext(BUNDLE.slice(start, BUNDLE.indexOf('\n})();', start) + 6), sandbox);
	return { render: sandbox.window.gunstore.renderReleaseNotes, frappe };
}
const showdown = () => load(require(SHOWDOWN).Converter);
const html = (text) => showdown().render([['1.0.0', text]]);

test('every note goes through frappe.markdown, newest first as given, joined by <hr>; no notes → empty', () => {
	const { render, frappe } = load(RecordingConverter);
	assert.strictEqual(render([['0.3.0', 'third'], ['0.2.0', 'second']]), '<div class="text-left app-change-log"><p>third</p><hr><p>second</p></div>');
	assert.deepStrictEqual(frappe.md2html.calls.filter((c) => c.text).map((c) => c.text), ['third', 'second'], 'the notes as written');
	assert.strictEqual(render([]), '');
	assert.strictEqual(render(null), '');
	assert.strictEqual(render([['1.0.0', null]]), '<div class="text-left app-change-log"><p></p></div>');
});

test("literalMidWordUnderscores is on for renderReleaseNotes' own call only: the shared converter gets its option back, a throw included", () => {
	const { render, frappe } = load(RecordingConverter);
	render([['0.2.0', 'first'], ['0.1.0', 'second']]);
	assert.ok(frappe.md2html.calls.filter((c) => c.text).every((c) => c.literalMidWordUnderscores === true), 'every note rendered with it');
	assert.strictEqual(frappe.md2html.getOption('literalMidWordUnderscores'), undefined, 'put back as it was (unset)');
	frappe.markdown('elsewhere');
	assert.strictEqual(frappe.md2html.calls.at(-1).literalMidWordUnderscores, undefined, "frappe's other markdown keeps its own reading");
	frappe.md2html.setOption('literalMidWordUnderscores', true);
	render([['0.3.0', 'third']]);
	assert.strictEqual(frappe.md2html.getOption('literalMidWordUnderscores'), true, 'a value set before is kept, not reset to false');
	frappe.md2html.setOption('literalMidWordUnderscores', false);
	assert.throws(() => render([['0.4.0', 'boom']]), /boom/);
	assert.strictEqual(frappe.md2html.getOption('literalMidWordUnderscores'), false, 'restored when a note throws');
});

test("with frappe's showdown: snake_case stays literal, _em_ and __strong__ stay markdown, code is left alone", needsShowdown, () => {
	assert.strictEqual(html('- Bundles ext_hello_dep 1.0.0 (see `ext_hello_dep`).'),
		'<div class="text-left app-change-log"><ul>\n<li>Bundles ext_hello_dep 1.0.0 (see <code>ext_hello_dep</code>).</li>\n</ul></div>');
	assert.strictEqual(html('_new_ and __really new__, snake_case, Größe_neu 1_0'),
		'<div class="text-left app-change-log"><p><em>new</em> and <strong>really new</strong>, snake_case, Größe_neu 1_0</p></div>');
	assert.strictEqual(html('```py\nsnake_case_name = 1\n```\nafter snake_case'),
		'<div class="text-left app-change-log"><pre><code class="py language-py">snake_case_name = 1\n</code></pre>\n<p>after snake_case</p></div>');
	assert.strictEqual(html('Example:\n\n    indented_code = snake_case'),
		'<div class="text-left app-change-log"><p>Example:</p>\n<pre><code>indented_code = snake_case\n</code></pre></div>');
});

test("with frappe's showdown: inline HTML and reference ids keep their underscores (review-pipeline r1 nit)", needsShowdown, () => {
	const link = '<a href="https://example.com/some_page" title="point_of_sale">the page</a>';
	assert.strictEqual(html(`See ${link} for snake_case.`), `<div class="text-left app-change-log"><p>See ${link} for snake_case.</p></div>`);
	const out = html('See [the qa_ref page][qa_ref], [qa_ref][] or [QA_Ref].\n\n[qa_ref]: https://example.com/qa_ref_page');
	assert.strictEqual(out, '<div class="text-left app-change-log"><p>See <a href="https://example.com/qa_ref_page">the qa_ref page</a>, '
		+ '<a href="https://example.com/qa_ref_page">qa_ref</a> or <a href="https://example.com/qa_ref_page">QA_Ref</a>.</p></div>');
});

test("with frappe's showdown: the converter What's New shares still reads mid-word underscores its own way afterwards", needsShowdown, () => {
	const { render, frappe } = showdown();
	render([['1.0.0', 'ext_hello_dep']]);
	assert.strictEqual(frappe.markdown('ext_hello_dep'), '<p>ext<em>hello</em>dep</p>', "frappe's default, untouched");
});

test("with frappe's showdown: the POS's own What's New notes render with no mid-word emphasis and no backslash", needsShowdown, () => {
	const files = NOTES.filter((f) => f.endsWith('.md')).sort();
	assert.ok(files.length >= 1, files);
	for (const file of files) {
		const out = html(fs.readFileSync(path.join(CHANGE_LOG, file), 'utf8'));
		assert.doesNotMatch(out, /[\p{L}\p{N}]<(em|strong)>|<\/(em|strong)>[\p{L}\p{N}]/u, file);
		assert.doesNotMatch(out, /\\_/, file);
	}
});

test("What's New renders its entries with the shared function", () => {
	assert.match(BUNDLE, /window\.gunstore\.showWhatsNew = function \(\) \{[^]*?window\.gunstore\.renderReleaseNotes\(entries\)/);
	assert.strictEqual(BUNDLE.split('window.gunstore.renderReleaseNotes = ').length, 2, 'defined once, in the releaseNotes block');
	assert.ok(!/literalUnderscores\(/.test(BUNDLE), 'no hand-written escape left');
});
