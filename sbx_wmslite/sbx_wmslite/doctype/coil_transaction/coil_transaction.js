// A Coil Transaction is an immutable record of one PAST scan — it deliberately
// does not carry the coil's current state, and for an RM row it does not even
// carry a location (bin_code stays blank on purpose; see rm_api's docstring).
//
// So "is this coil in transit or received?" is unanswerable from the fields on
// this form, and the honest place to read it is the coil's Bin Inventory row.
// Rather than expect everyone to know that, fetch it live and show it up top.
//
// Fetched on refresh rather than stored: a stored copy would be a snapshot taken
// at insert time and would start lying the moment the coil moved again — which is
// exactly the bug this ledger design exists to avoid.
frappe.ui.form.on("Coil Transaction", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.coil_barcode) return;

		sapPushControls(frm);

		frappe.db.get_value("Bin Inventory", frm.doc.coil_barcode,
			["rm_status", "rm_destination", "rm_received_at", "rm_dispatched_at",
			 "rm_unplanned", "bin_code"]
		).then((r) => {
			const st = (r && r.message) || {};
			if (!st.bin_code && !st.rm_status) {
				// No Bin Inventory row at all — the coil has never been placed.
				frm.dashboard.set_headline_alert(
					__("This coil has no inventory record — nothing knows where it is."), "red");
				return;
			}

			if (st.rm_status === "In Transit to Line") {
				frm.dashboard.set_headline_alert(
					__("Now: in transit to {0}{1} — not yet received", [
						st.rm_destination || "?",
						st.rm_dispatched_at ? " · sent " + frappe.datetime.str_to_user(st.rm_dispatched_at) : "",
					]), "orange");
			} else if (st.rm_status === "Received") {
				frm.dashboard.set_headline_alert(
					__("Now: received at {0}{1}{2}", [
						st.rm_destination || "?",
						st.rm_received_at ? " · " + frappe.datetime.str_to_user(st.rm_received_at) : "",
						st.rm_unplanned ? " · " + __("FLAGGED — arrived where it was not sent") : "",
					]), st.rm_unplanned ? "red" : "green");
			} else {
				// Not on an RM movement: the useful "now" is the warehouse bin.
				frm.dashboard.set_headline_alert(
					__("Now: in bin {0}", [st.bin_code || "?"]), "blue");
			}

			// One click through to the row that actually holds the live state.
			frm.add_custom_button(__("Coil's current state"), () => {
				frappe.set_route("Form", "Bin Inventory", frm.doc.coil_barcode);
			});
		});
	},
});

// SAP push — two kinds of in-app scan are pushed, each by its own module:
//   Inventory Count  -> T1, LOCATION = bin   (count_push)
//   RM Receipt       -> T2, LOCATION = line  (rm_push)
// An indicator (not the headline: that is the coil's live location, set above)
// plus Push Now, which delegates to that module's trigger_push; sap_status is
// the engine the sweep acts on. Mirrors wmslite_sap_loading.js.
const SAP_PUSH_METHOD = {
	"Inventory Count": "sbx_wmslite.count_push.trigger_push",
	"RM Receipt": "sbx_wmslite.rm_push.trigger_push",
};

function sapPushControls(frm) {
	const d = frm.doc;
	const method = SAP_PUSH_METHOD[d.transaction_type];
	if (!method || d.source !== "In-app") return;

	// A blank status is a scan that was never queued (made before the push
	// existed, or while the web process ran older code): show it as such — it is
	// still pushable below.
	const status = d.sap_status || "Not queued";
	const color = { Sent: "green", Failed: "red", Disabled: "orange", Pending: "blue",
					Sending: "blue", "Not Required": "gray", "Not queued": "orange" }[status] || "gray";
	const label = status === "Sent" && d.sap_pushed_at
		? __("SAP: Sent · {0}", [frappe.datetime.str_to_user(d.sap_pushed_at)])
		: __("SAP: {0}", [status]);
	const ind = frm.dashboard.add_indicator(label, color);
	if (ind && d.sap_message) ind.attr("title", d.sap_message);

	if (["", "Pending", "Failed", "Disabled"].includes(d.sap_status || "") && !d.reversed) {
		frm.add_custom_button(__("Push Now"), () => {
			frappe.call({
				method: method,
				args: { txn: d.name },
				freeze: true, freeze_message: __("Queuing push…"),
			}).then((r) => {
				const res = (r && r.message) || {};
				if (res.ok) frappe.show_alert({ message: __("Queued — refreshing shortly"), indicator: "blue" });
				else frappe.msgprint(__("Not queued: {0}", [res.skipped || "unknown"]));
				setTimeout(() => frm.reload_doc(), 1500);
			});
		}).addClass("btn-primary");
	}
}
