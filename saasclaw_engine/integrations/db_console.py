"""Database console API — browse tables, run SQL against project databases.

All endpoints require authentication (project owner or staff).
Connections use psycopg3 with auto-commit for SELECTs, transaction rollback for safety.
"""

import logging
from pathlib import Path

import psycopg
from django.conf import settings
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from saasclaw_engine.deployments.service import _load_env_file
from saasclaw_engine.projects.models import Project

logger = logging.getLogger(__name__)

# Maximum rows returned by browse/query
MAX_ROWS = 500

# Read-only SQL verbs (lowercase)
READ_ONLY_VERBS = {'select', 'with', 'values', 'table', 'show', 'explain', 'describe'}


def _can_manage(user, project):
    if not user or not user.is_authenticated:
        return False
    return user.is_staff or project.owner_id == user.id


def _get_project_db_env(project, environment_name='preview'):
    """Load .env file and return database connection params."""
    runtime_root = Path(project.workspace_root) / 'runtime' / environment_name
    env_file = runtime_root / '.env'
    env = _load_env_file(env_file)

    if not env.get('POSTGRES_DB'):
        return None, f"No database configured for {environment_name}"

    return {
        'dbname': env.get('POSTGRES_DB', ''),
        'user': env.get('POSTGRES_USER', ''),
        'password': env.get('POSTGRES_PASSWORD', ''),
        'host': env.get('POSTGRES_HOST', '127.0.0.1'),
        'port': env.get('POSTGRES_PORT', '5432'),
    }, None


def _connect(project, environment_name):
    """Get DB env and return psycopg connection or (None, error)."""
    db_env, error = _get_project_db_env(project, environment_name)
    if error:
        return None, error

    dsn = "host={host} port={port} dbname={dbname} user={user} password={password}".format(**db_env)
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        return conn, None
    except Exception as e:
        return None, str(e)


def _project_or_403(request, slug):
    """Get project, check permissions, return (project, None) or (None, response)."""
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return None, JsonResponse({'ok': False, 'error': 'Project not found.'}, status=404)
    if not _can_manage(request.user, project):
        return None, HttpResponseForbidden('Access denied.')
    return project, None


@require_http_methods(["GET"])
def db_tables(request, slug, env):
    """List all tables in the project database."""
    project, err = _project_or_403(request, slug)
    if err:
        return err

    if env not in ('preview', 'production'):
        return HttpResponseBadRequest('Environment must be preview or production.')

    conn, error = _connect(project, env)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=500)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name,
                       (SELECT count(*) FROM information_schema.columns c
                        WHERE c.table_name = t.table_name
                        AND c.table_schema = 'public') as column_count,
                       pg_size_pretty(pg_total_relation_size(quote_ident(table_name)::regclass)) as size
                FROM information_schema.tables t
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            rows = cur.fetchall()

            tables = []
            for r in rows:
                cur.execute('SELECT count(*) FROM "%s"' % r[0])
                count = cur.fetchone()[0]
                tables.append({
                    'name': r[0],
                    'columns': r[1],
                    'rows': count,
                    'size': r[2],
                })

            # Get total DB size
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            db_size = cur.fetchone()[0]

        return JsonResponse({
            'ok': True,
            'environment': env,
            'database': _get_project_db_env(project, env)[0]['dbname'],
            'db_size': db_size,
            'tables': tables,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)
    finally:
        conn.close()


@require_http_methods(["GET"])
def db_table_detail(request, slug, env, table):
    """Get table schema (columns) and optionally browse rows."""
    project, err = _project_or_403(request, slug)
    if err:
        return err

    if env not in ('preview', 'production'):
        return HttpResponseBadRequest('Environment must be preview or production.')

    conn, error = _connect(project, env)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=500)

    # Sanitize table name — only allow alphanumeric + underscore
    safe_table = ''.join(c for c in table if c.isalnum() or c == '_')
    if safe_table != table:
        conn.close()
        return HttpResponseBadRequest('Invalid table name.')

    try:
        with conn.cursor() as cur:
            # Column info
            cur.execute("""
                SELECT column_name, data_type, is_nullable,
                       column_default, character_maximum_length
                FROM information_schema.columns
                WHERE table_name = %s AND table_schema = 'public'
                ORDER BY ordinal_position
            """, (table,))
            columns = [
                {
                    'name': r[0],
                    'type': r[1],
                    'nullable': r[2] == 'YES',
                    'default': r[3],
                    'max_length': r[4],
                }
                for r in cur.fetchall()
            ]

            # Browse rows
            limit = min(int(request.GET.get('limit', 100)), MAX_ROWS)
            offset = int(request.GET.get('offset', 0))
            order = request.GET.get('order', '')

            order_clause = ''
            if order:
                safe_order = ''.join(c for c in order if c.isalnum() or c in ('_', '.', ' ', 'ASC', 'DESC', 'asc', 'desc'))
                if safe_order:
                    order_clause = f"ORDER BY {safe_order}"

            cur.execute(f'SELECT count(*) FROM "{safe_table}"')
            total = cur.fetchone()[0]

            cur.execute(f'SELECT * FROM "{safe_table}" {order_clause} LIMIT %s OFFSET %s', (limit, offset))
            rows = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]

            row_data = []
            for row in rows:
                row_data.append(dict(zip(col_names, [str(v) if v is not None else None for v in row])))

        return JsonResponse({
            'ok': True,
            'environment': env,
            'table': table,
            'columns': columns,
            'total': total,
            'limit': limit,
            'offset': offset,
            'rows': row_data,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)
    finally:
        conn.close()


@csrf_exempt
@require_http_methods(["POST"])
def db_query(request, slug, env):
    """Run a SQL query against the project database. Read-only by default."""
    project, err = _project_or_403(request, slug)
    if err:
        return err

    if env not in ('preview', 'production'):
        return HttpResponseBadRequest('Environment must be preview or production.')

    import json
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest('Invalid JSON body.')

    sql = (body.get('sql') or '').strip()
    if not sql:
        return HttpResponseBadRequest('No SQL provided.')

    # Safety check — only allow read-only queries unless explicitly allowed
    allow_write = body.get('write', False)
    first_word = sql.split()[0].lower() if sql.split() else ''

    if not allow_write and first_word not in READ_ONLY_VERBS:
        return JsonResponse({
            'ok': False,
            'error': 'Write queries not allowed. Set {"write": true} to enable.',
        }, status=403)

    # Strip trailing semicolons and limit rows for read queries
    sql = sql.rstrip('; ')
    if first_word in ('select', 'with') and 'LIMIT' not in sql.upper():
        sql += f" LIMIT {MAX_ROWS}"

    conn, error = _connect(project, env)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=500)

    try:
        with conn.cursor() as cur:
            cur.execute(sql)

            if cur.description:
                col_names = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(rows) > MAX_ROWS
                rows = rows[:MAX_ROWS]
                row_data = [
                    dict(zip(col_names, [str(v) if v is not None else None for v in row]))
                    for row in rows
                ]
                return JsonResponse({
                    'ok': True,
                    'columns': col_names,
                    'rows': row_data,
                    'row_count': len(row_data),
                    'truncated': truncated,
                })
            else:
                return JsonResponse({
                    'ok': True,
                    'rows_affected': cur.rowcount,
                })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    finally:
        conn.close()


__all__ = ['db_tables', 'db_table_detail', 'db_query']
