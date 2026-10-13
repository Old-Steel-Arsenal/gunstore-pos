// Redoc bootstrap — external file (not inline) because index.html's CSP
// deliberately has no 'unsafe-inline' in script-src: the policy that blocks
// the bundle's cdn.redoc.ly phone-home would also block an inline initializer.
Redoc.init(
  "./openapi.json",
  {
    disableGoogleFont: true,
    expandResponses: "200",
    theme: {
      typography: {
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
        headings: { fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" },
        code: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" }
      }
    }
  },
  document.getElementById("redoc")
);
