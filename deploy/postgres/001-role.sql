-- Runs once on a fresh PostgreSQL volume; no application tables or user data imported.
\getenv app_user POSTGRES_APP_USER
\getenv app_password POSTGRES_APP_PASSWORD
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'app_user', :'app_password') \gexec
SELECT format('GRANT CREATE ON DATABASE %I TO %I', current_database(), :'app_user') \gexec
