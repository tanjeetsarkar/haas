# HaaS Backup and Disaster Recovery Runbook

## Overview

This runbook covers Point-in-Time Recovery (PITR), backup procedures, and disaster recovery drills for the HaaS platform. The platform uses PostgreSQL as the authoritative data store with WAL archiving for PITR capability.

## Architecture

```
┌─────────────────┐     WAL Archive      ┌──────────────────┐
│  Primary PG     │ ──────────────────▶  │  WAL Archive     │
│  (haas-db)      │   (continuous)       │  (S3/GCS/Azure)  │
└─────────────────┘                      └──────────────────┘
        │                                        │
        │ Base Backup (daily)                    │
        ▼                                        ▼
┌─────────────────┐                      ┌──────────────────┐
│  Base Backup    │                      │  Recovery Target │
│  (pg_basebackup)│                      │  (PITR restore)  │
└─────────────────┘                      └──────────────────┘
```

## Backup Strategy

### Continuous WAL Archiving
- **Method**: `archive_command` in `postgresql.conf`
- **Destination**: S3-compatible object store (versioned bucket)
- **Retention**: 30 days of WAL files
- **Monitoring**: `pg_stat_archiver` metrics

### Daily Base Backups
- **Tool**: `pg_basebackup` with `--format=tar --gzip`
- **Schedule**: Daily at 02:00 UTC
- **Retention**: 7 daily + 4 weekly + 12 monthly
- **Verification**: Automated restore test weekly

### Backup Validation
```bash
# Verify backup integrity
pg_verifybackup /path/to/backup

# Test restore to staging
pg_restore --dry-run --format=tar /path/to/backup.tar.gz
```

## PITR Procedure

### Prerequisites
1. Access to WAL archive bucket
2. Base backup from before target time
3. Target recovery timestamp (UTC)
4. Clean PostgreSQL data directory

### Recovery Steps

```bash
# 1. Stop PostgreSQL
systemctl stop postgresql

# 2. Clear data directory
rm -rf /var/lib/postgresql/data/*

# 3. Restore base backup
tar -xzf /backups/base/haas-base-20240115.tar.gz -C /var/lib/postgresql/data/

# 4. Create recovery.signal
touch /var/lib/postgresql/data/recovery.signal

# 5. Configure restore_command in postgresql.auto.conf
cat >> /var/lib/postgresql/data/postgresql.auto.conf <<EOF
restore_command = 'aws s3 cp s3://haas-wal-archive/%f %p'
recovery_target_time = '2024-01-15 14:30:00 UTC'
recovery_target_action = 'promote'
EOF

# 6. Start PostgreSQL
systemctl start postgresql

# 7. Monitor recovery
tail -f /var/log/postgresql/postgresql-*.log | grep -E "(recovery|restored|promote)"
```

### Recovery Target Options

| Option | Use Case |
|--------|----------|
| `recovery_target_time` | Point-in-time to specific timestamp |
| `recovery_target_xid` | Recover to specific transaction |
| `recovery_target_lsn` | Recover to specific LSN |
| `recovery_target_name` | Recover to named restore point |

## Restore Epoch Fencing

After PITR restore, the platform must fence all in-flight runs and reset webhook deliveries. This is handled by the `/v1/ops/restore-epoch` endpoint:

```bash
# Bump restore epoch (fences runs, resets deliveries)
curl -X POST https://api.haas.example/v1/ops/restore-epoch \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-ID: $TENANT" \
  -d '{"reason": "PITR restore to 2024-01-15 14:30:00 UTC", "actor": "ops-oncall"}'
```

This triggers:
1. Increment `platform_state.restore_epoch`
2. Fence all running runs (`lease_epoch++`, `lease_expires_at = now() - 1s`)
3. Reset in-flight webhook deliveries to `pending`
4. Create `restore_events` audit record

## Disaster Recovery Drills

### Monthly Drill Schedule
- **Week 1**: Full PITR restore to staging environment
- **Week 2**: Partial restore (single tenant schema)
- **Week 3**: Cross-region restore test
- **Week 4**: Failover to standby (if configured)

### Drill Checklist

```markdown
## Pre-Drill
- [ ] Notify stakeholders (24h advance)
- [ ] Verify WAL archive accessibility
- [ ] Confirm base backup integrity
- [ ] Prepare clean staging environment
- [ ] Document target recovery point

## During Drill
- [ ] Execute PITR procedure
- [ ] Verify restore epoch bump
- [ ] Validate tenant data integrity
- [ ] Test API connectivity
- [ ] Run smoke tests against restored data
- [ ] Measure RTO (Recovery Time Objective)

## Post-Drill
- [ ] Document actual RTO/RPO
- [ ] Compare against SLO targets
- [ ] Update runbook if needed
- [ ] Clean up staging environment
- [ ] Report to stakeholders
```

### SLO Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| RPO (Recovery Point Objective) | < 5 minutes | WAL archive lag |
| RTO (Recovery Time Objective) | < 30 minutes | Drill stopwatch |
| Backup Success Rate | 100% | Daily verification |
| Restore Test Success | 100% | Monthly drill |

## Cross-Region DR

For multi-region deployments:

1. **Primary Region**: Active read/write
2. **Standby Region**: Streaming replication + WAL archive
3. **Failover**: Promote standby, update DNS, bump restore epoch

```bash
# Promote standby
pg_ctl promote -D /var/lib/postgresql/data

# Update DNS to point to standby
# (Terraform/Route53/CloudDNS automation)

# Bump restore epoch on new primary
curl -X POST https://api.haas.example/v1/ops/restore-epoch \
  -d '{"reason": "DR failover to standby region", "actor": "ops-oncall"}'
```

## Monitoring and Alerting

### Key Metrics
- `pg_stat_archiver.archived_count` - WAL files archived
- `pg_stat_archiver.failed_count` - Archive failures
- `pg_replication_slots.restart_lsn` - Replication lag
- `haas_restore_epoch` - Current restore epoch (from `/v1/ops/metrics`)

### Alert Rules
```yaml
# WAL archive failure
- alert: PGWALArchiveFailure
  expr: rate(pg_stat_archiver_failed_count[5m]) > 0
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "PostgreSQL WAL archiving failing"

# Replication lag
- alert: PGReplicationLag
  expr: pg_wal_lsn_diff(pg_current_wal_lsn(), pg_replication_slots.restart_lsn) > 100000000
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "PostgreSQL replication lag > 100MB"

# Restore epoch unexpected change
- alert: HAASRestoreEpochChange
  expr: changes(haas_restore_epoch[1h]) > 0
  labels:
    severity: info
  annotations:
    summary: "HaaS restore epoch changed - investigate"
```

## Runbook Maintenance

- **Review**: Quarterly
- **Update**: After each drill or incident
- **Version**: Track in git with infrastructure changes
- **Approval**: Platform team lead + Security team

## References

- [PostgreSQL PITR Documentation](https://www.postgresql.org/docs/current/continuous-archiving.html)
- [HaaS Operations Migration](design/operations-migration.md)
- [Platform State Schema](migrations/versions/0015_service_operations.py)
- [Restore Epoch API](app/api/operations.py)