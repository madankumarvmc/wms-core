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
