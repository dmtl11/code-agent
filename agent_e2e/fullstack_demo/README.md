# Echo App

A minimal full-stack web app with **no third-party dependencies**.

- **Backend**: Python standard library only (`http.server`).
- **Frontend**: plain HTML, CSS, and JavaScript.

## Endpoints

| Method | Path          | Description                          |
|--------|---------------|--------------------------------------|
| GET    | `/api/health` | Returns `{"status": "ok"}`.          |
| POST   | `/api/echo`   | Echoes back the JSON body you send.  |
| GET    | `/`           | Serves the frontend page.            |

## Run

```bash
python server.py
```

Then open <http://127.0.0.1:8000> in your browser.

## Test

```bash
python -m unittest test_server -v
```

## Project layout

```
.
├── server.py          # HTTP server (stdlib only)
├── static/
│   ├── index.html     # Frontend page
│   ├── style.css      # Styles
│   └── app.js         # Frontend logic (fetch /api/echo)
├── test_server.py     # unittest tests
└── README.md
```
