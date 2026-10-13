// The only thing an extension's bundle does: register with the core registry
// The core renders the button on the POS toolbar for
// switched-on extensions and hides it otherwise — no DOM work here.
gunstore.extensions.register('ext_hello', {
	posActions: [{
		id: 'ext-hello-greet',
		label: __('Say hi'),
		condition: (ctx) => !!(ctx.invoice && ctx.invoice.customer),
		onClick: (ctx) => frappe.xcall('ext_hello.api.greet.greet', { customer: ctx.invoice.customer })
			.then((r) => frappe.show_alert({ message: r.message, indicator: 'green' })),
	}],
	settingsLinks: [],
});
