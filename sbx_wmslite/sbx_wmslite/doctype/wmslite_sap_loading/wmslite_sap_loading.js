// T1 loading push — tracking + control. The buttons delegate to loading_push;
// the coil's sap_loading_status remains the engine that sweep() acts on.
frappe.ui.form.on("WMSLite SAP Loading", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status === "Sent") {
			frm.dashboard.set_headline_alert(
				"Sent to SAP" + (frm.doc.last_pushed_at ? " · " + frm.doc.last_pushed_at : ""), "green");
		} else if (frm.doc.status === "Failed") {
			frm.dashboard.set_headline_alert(
				"Failed: " + (frm.doc.sap_message || frm.doc.last_error || "see below"), "red");
		} else if (frm.doc.status === "Disabled") {
			frm.dashboard.set_headline_alert(
				"Recorded but not sent — SAP loading push was OFF when this coil loaded. " +
				"Enable it in WMSLite Settings, then Push Now.", "orange");
		}

		// Push Now — queue this coil and run the sweep immediately.
		if (["Pending", "Failed", "Not Required", "Disabled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Push Now"), () => {
				frappe.call({
					method: "sbx_wmslite.loading_push.trigger_push",
					args: { loading: frm.doc.name },
					freeze: true, freeze_message: __("Queuing push…"),
				}).then((r) => {
					const res = (r && r.message) || {};
					if (res.ok) frappe.show_alert({ message: __("Queued — refreshing shortly"), indicator: "blue" });
					else frappe.msgprint(__("Not queued: {0}", [res.skipped || "unknown"]));
					setTimeout(() => frm.reload_doc(), 1500);
				});
			}).addClass("btn-primary");
		}
	},
});
