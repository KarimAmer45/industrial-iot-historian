"""Generate ignored local Grafana settings and a dedicated dashboard login limited to the panel views."""
import json,pathlib,secrets,sys
from urllib.parse import urlparse
from psycopg import sql
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ingest'))
from common import connect

def main():
    import os
    path=ROOT/'.local/grafana-secrets.json'
    for directory in ('grafana-data','grafana-logs'):(ROOT/'.local'/directory).mkdir(exist_ok=True)
    secrets_data=json.loads(path.read_text()) if path.exists() else {'db_password':secrets.token_urlsafe(24),'admin_password':secrets.token_urlsafe(24)}
    path.write_text(json.dumps(secrets_data),encoding='utf-8')
    with connect('grafana_setup') as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='historian_dashboard'").fetchone():
            conn.execute(sql.SQL('CREATE ROLE historian_dashboard LOGIN PASSWORD {}').format(sql.Literal(secrets_data['db_password'])))
        # role_dashboard reads only what the six panels query; earlier installations granted role_readonly.
        conn.execute('GRANT role_dashboard TO historian_dashboard')
        if conn.execute("SELECT pg_has_role('historian_dashboard','role_readonly','MEMBER')").fetchone()[0]:
            conn.execute('REVOKE role_readonly FROM historian_dashboard')
    uri=urlparse(os.environ['DATABASE_URL'])
    env={'HIST_DB_HOST':f'{uri.hostname}:{uri.port or 5432}','HIST_DB_USER':'historian_dashboard','HIST_DB_PASSWORD':secrets_data['db_password'],'HIST_DB_NAME':uri.path[1:],
      'HIST_DASHBOARDS_PATH':str(ROOT/'grafana/provisioning/dashboards'),'GF_PATHS_PROVISIONING':str(ROOT/'grafana/provisioning'),
      'GF_PATHS_DATA':str(ROOT/'.local/grafana-data'),'GF_PATHS_LOGS':str(ROOT/'.local/grafana-logs'),
      'GF_SERVER_HTTP_ADDR':'127.0.0.1','GF_SERVER_HTTP_PORT':'3003','GF_SECURITY_ADMIN_PASSWORD':secrets_data['admin_password'],
      'GF_AUTH_ANONYMOUS_ENABLED':'true','GF_AUTH_ANONYMOUS_ORG_ROLE':'Viewer','GF_USERS_ALLOW_SIGN_UP':'false',
      'GF_ANALYTICS_REPORTING_ENABLED':'false','GF_ANALYTICS_CHECK_FOR_UPDATES':'false'}
    (ROOT/'.local/grafana-env.json').write_text(json.dumps(env),encoding='utf-8')
    print('Configured loopback-only Grafana and dedicated dashboard database login')
if __name__=='__main__':main()
