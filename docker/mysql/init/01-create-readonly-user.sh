#!/bin/bash
set -e

# MySQL 官方镜像只在数据目录首次初始化时执行本脚本。在线查询使用该账号，
# Alembic 和模拟数据灌入则使用 MYSQL_USER 对应的管理账号。
mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<-EOSQL
CREATE USER IF NOT EXISTS '${MYSQL_READ_USER}'@'%' IDENTIFIED BY '${MYSQL_READ_PASSWORD}';
GRANT SELECT ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_READ_USER}'@'%';
FLUSH PRIVILEGES;
EOSQL
