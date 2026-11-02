# Map tiles come from OpenStreetMap; Leaflet sets inline styles on its elements.
add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'" always;
add_header X-Content-Type-Options "nosniff" always;
# Cross-origin requests carry only the origin (never the path). OpenStreetMap
# requires a Referer on tile requests and serves an "Access blocked" image
# without one, which "same-origin" (no Referer cross-origin) caused.
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
