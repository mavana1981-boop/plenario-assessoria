[2026-05-22 22:16:11 +0000] [1] [INFO] Starting gunicorn 22.0.0
2026-05-22 22:16:11,548 - INFO - ✅ PostgreSQL configurado: host=postgres.railway.internal db=railway
/app/.venv/lib/python3.13/site-packages/requests/__init__.py:113: RequestsDependencyWarning: urllib3 (2.7.0) or chardet (7.4.3)/charset_normalizer (3.4.7) doesn't match a supported version!
[2026-05-22 22:16:11 +0000] [1] [INFO] Listening at: http://0.0.0.0:8080 (1)
  warnings.warn(
[2026-05-22 22:16:11 +0000] [2] [INFO] Booting worker with pid: 2
/app/.venv/lib/python3.13/site-packages/requests/__init__.py:113: RequestsDependencyWarning: urllib3 (2.7.0) or chardet (7.4.3)/charset_normalizer (3.4.7) doesn't match a supported version!
[2026-05-22 22:16:11 +0000] [3] [INFO] Booting worker with pid: 3
2026-05-22 22:16:12,116 - INFO - ✅ Banco inicializado (PostgreSQL).
2026-05-22 22:16:12,466 - INFO - ✅ Banco inicializado (PostgreSQL).
2026-05-22 22:16:16,096 - INFO - REQ sem análise: REQ 2976/2026 ao PL 1448/2026
2026-05-22 22:16:16,096 - INFO - Índice itens_por_codigo: ['REQ471/2024', 'REQ2869/2026', 'REQ2973/2026', 'REQ2976/2026', 'MPV1334/2026', 'PL1625/2026', 'PL2766/2021', 'PL699/2023', 'PL2951/2024', 'PL2564/2025']
2026-05-22 22:16:19,352 - ERROR - Unhandled exception: 404 Not Found: The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 880, in full_dispatch_request
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 865, in dispatch_request
    return self.ensure_sync(self.view_functions[rule.endpoint])(**view_args)  # type: ignore[no-any-return]
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 270, in <lambda>
                           ~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
    return send_from_directory(
        t.cast(str, self.static_folder), filename, max_age=max_age
  File "/app/.venv/lib/python3.13/site-packages/flask/helpers.py", line 552, in send_from_directory
    return werkzeug.utils.send_from_directory(  # type: ignore[return-value]
        directory, path, **_prepare_send_file_kwargs(**kwargs)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ^
    raise NotFound()
werkzeug.exceptions.NotFound: 404 Not Found: The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.
    )
  File "/app/.venv/lib/python3.13/site-packages/flask/helpers.py", line 552, in send_from_directory
Traceback (most recent call last):
    return werkzeug.utils.send_from_directory(  # type: ignore[return-value]
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 880, in full_dispatch_request
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 865, in dispatch_request
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 270, in <lambda>
    view_func=lambda **kw: self_ref().send_static_file(**kw),  # type: ignore # noqa: B950
                           ~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/app/.venv/lib/python3.13/site-packages/flask/app.py", line 318, in send_static_file
    return send_from_directory(
        t.cast(str, self.static_folder), filename, max_age=max_age
        directory, path, **_prepare_send_file_kwargs(**kwargs)
    )
    ^
  File "/app/.venv/lib/python3.13/site-packages/werkzeug/utils.py", line 568, in send_from_directory
    raise NotFound()
werkzeug.exceptions.NotFound: 404 Not Found: The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again.
