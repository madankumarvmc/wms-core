app_name = "sbx_wmslite"
app_title = "SBX WMSLite"
app_publisher = "StackBox"
app_description = "Lite WMS truck-loading for steel coil dispatch"
app_email = "madankumar@stackbox.xyz"
app_license = "agpl-3.0"

# Apps
# ------------------

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": "sbx_wmslite",
		"title": "SBX WMSLite",
		"route": "/wms-loading",
	}
]

# Includes in <head>
# ------------------

# Stackbox login reskin — every rule is scoped to .for-login (only exists on /login)
web_include_css = "/assets/sbx_wmslite/css/stackbox-login.css"

# Installation
# ------------
after_install = "sbx_wmslite.install.after_install"

# Scheduled Tasks
# ---------------
# Drive outbound SAP goods-issue retries (backoff) + recover stuck sends.
scheduler_events = {
	"cron": {
		"*/3 * * * *": [
			"sbx_wmslite.outbound_sap.retry_sweep",
		]
	}
}
