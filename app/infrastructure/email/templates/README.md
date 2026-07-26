# Email templates

Rendered by [`providers.render_template`](../providers.py). The directory is
configured by `EMAIL__TEMPLATE_DIR`.

## Conventions

Each message is **two files** sharing a base name:

```
welcome.html    # rich body
welcome.txt     # plain-text fallback — never omit it
```

A message referencing `template="welcome"` renders both. Sending HTML only gets
messages flagged as spam and renders as blank in text-only clients.

## Rules

- **No business logic in templates.** A template interpolates the context it is
  given; it never queries the database or reaches into ORM objects. Services
  build a plain `dict` and pass it in.
- **Escape everything.** Any user-supplied value is untrusted; template
  auto-escaping must stay on.
- **Absolute URLs only.** There is no base URL in an inbox — build links from
  configuration, never as relative paths.
- **Inline the CSS.** Mail clients strip `<style>` blocks and never fetch
  external stylesheets.
- **No tracking pixels or remote images without consent.** Most clients block
  them, and in several jurisdictions they require opt-in.

## Layout

Shared chrome (header, footer, unsubscribe block) belongs in `_layout.html`,
which concrete templates extend. Prefix partials with `_` so they are never
mistaken for a sendable template.

## Testing

Every template needs a rendering test asserting both parts render with a
representative context and that no placeholder is left unsubstituted. See
[`tests/README.md`](../../../../tests/README.md).
