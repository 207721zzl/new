"""Create local deployment secrets once, without printing them or overwriting existing config."""
from pathlib import Path
import secrets


def main():
    path=Path('.env.microservices')
    from dotenv import dotenv_values
    existing=dotenv_values(path) if path.exists() else {}
    values={key:secrets.token_urlsafe(32) for key in ["INTERNAL_SERVICE_TOKEN","MICRO_MYSQL_ROOT_PASSWORD",
        "IDENTITY_DB_PASSWORD","CHAT_DB_PASSWORD","KNOWLEDGE_DB_PASSWORD","OBJECT_SECRET_KEY",
        "IDENTITY_MIGRATION_PASSWORD","CHAT_MIGRATION_PASSWORD","KNOWLEDGE_MIGRATION_PASSWORD"]}
    values.update({
        "OBJECT_ACCESS_KEY": "evidence_uploads",
        "MICRO_API_PORT": "18000",
        "MICRO_MYSQL_PORT": "23306",
        "MICRO_BIND_HOST": "127.0.0.1",
    })
    missing={key:value for key,value in values.items() if not existing.get(key)}
    if missing:
        with path.open('a',encoding='utf-8') as stream:
            stream.write('\n'+'\n'.join(f'{key}={value}' for key,value in missing.items())+'\n')
    print('Local microservice settings ready; existing values retained and secrets not printed.')


if __name__=='__main__':
    main()
