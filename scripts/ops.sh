#!/usr/bin/env bash
# Operate the SportsArb instance on AWS: check the data, deploy, and manage the instance.
#
# The recorder runs as the systemd service sportsarb-recorder, which runs python3 -m live.run from
# ~/SportsArb/src, restarts on any exit, and logs to ~/SportsArb/data/record.log.
# The instance's details come from scripts/ops.env, see scripts/ops.env.example.
#
# Usage: scripts/ops.sh <command> [argument], or scripts/ops.sh help for the list.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -f "$HERE/ops.env" ]]; then
    echo "missing $HERE/ops.env, copy ops.env.example and fill it in" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$HERE/ops.env"
KEY="${SPORTSARB_KEY/#\~/$HOME}"
SERVICE=sportsarb-recorder
DB=SportsArb/data/sportsarb.sqlite
LOG=SportsArb/data/record.log

remote() {
    ssh -i "$KEY" "$SPORTSARB_USER@$SPORTSARB_HOST" "$@"
}

aws_ec2() {
    aws ec2 --region "$SPORTSARB_REGION" "$@"
}

one_hour_ago() {
    date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1H +%Y-%m-%dT%H:%M:%SZ
}

usage() {
    cat <<'USAGE'
Check the data
  health            Service state, last status line, drops, rows per venue, gaps, episodes by kind.
  summary [hours]   The summary report for the last hours, 24 by default.
  log               Follow the log live. Stop with ctrl-c.
  episodes          Episodes the scanner logged, and its ten minute summaries.
  paper             Executor, settlement, and transfer lines, then trades by outcome and open positions.
  largest           The largest episodes in the database.
  copy-db [dir]     Copy the database home for analysis, to data/aws by default. The recorder keeps running.

Run the recorder
  start             Start the recorder if it was stopped. It also starts on boot.
  stop              Stop the recorder.
  deploy            Pull, run the tests, and restart only if they pass. Restarting refreshes the catalog for about 80 seconds.
  fresh             Delete the database and start a new run. Asks first.

Instance
  shell             Open a shell on the instance.
  allow-ip          Allow SSH from this machine's current IP, needed when it changes, for example when a VPN toggles.
  instance          Instance state and public IP.
  cpu               CPU over the last hour, in five minute steps.
  disk              Free disk and the database's size.
  stop-instance     Stop the instance. Saves the compute cost and keeps the disk.
  start-instance    Start the instance. Its public IP changes, so update SPORTSARB_HOST in ops.env.
USAGE
}

command="${1:-help}"
case "$command" in
    health)
        remote "systemctl is-active $SERVICE; tail -1 $LOG | cut -c1-170; echo \"drops: \$(grep -c dropped $LOG)\"; sqlite3 $DB \"
            select venue, count(*), count(distinct contract_id), max(ts) from quotes group by 1;
            select venue, count(*), round(sum((julianday(end_ts) - julianday(start_ts)) * 86400)) as seconds_down from gaps group by 1;
            select kind, count(*), round(max(peak_profit), 2), sum(annual_pct >= 10) from opportunities o join pairs p on p.id = o.pair_id group by 1;\""
        ;;
    summary)
        remote "cd SportsArb && python3 src/tools/summary.py --hours ${2:-24}"
        ;;
    log)
        remote "tail -f $LOG"
        ;;
    episodes)
        remote "grep -E 'episode|scanner:' $LOG | tail -40"
        ;;
    paper)
        remote "grep -E 'paper|settled|transfer' $LOG | tail -30; sqlite3 -header -column $DB \"
            select status, count(*) as trades, sum(matched) as matched, round(sum(profit + hedge_pnl), 2) as net from trades group by 1;
            select venue, round(balance, 2) as balance from ledger where id in (select max(id) from ledger group by venue);
            select count(*) as open_trades from trades where settled_at is null and yes_held + no_held > 0;\""
        ;;
    largest)
        remote "sqlite3 -header -column $DB \"
            select p.label, trade, round(100 * peak_edge, 1) as edge_c, round(peak_size) as size, round(peak_profit, 2) as profit,
                   round(seconds) as secs, substr(peak_ts, 12, 8) as at_utc
            from opportunities o join pairs p on p.id = o.pair_id order by peak_profit desc limit 15\""
        ;;
    copy-db)
        dest="${2:-$HERE/../data/aws}"
        mkdir -p "$dest"
        rsync -avz -e "ssh -i $KEY" "$SPORTSARB_USER@$SPORTSARB_HOST:$DB*" "$dest/"
        ;;
    start|stop)
        remote "sudo systemctl $command $SERVICE && systemctl is-active $SERVICE || true"
        ;;
    deploy)
        remote "cd SportsArb && git pull --ff-only && if python3 -m pytest -q > /tmp/sportsarb-tests.log 2>&1; then
                    tail -1 /tmp/sportsarb-tests.log && sudo systemctl restart $SERVICE && echo restarted;
                else
                    tail -30 /tmp/sportsarb-tests.log; echo 'tests failed, the recorder was not restarted'; exit 1;
                fi"
        ;;
    fresh)
        read -r -p "This deletes the database on the instance and starts a new run. Type 'fresh' to go ahead: " answer
        if [[ "$answer" != "fresh" ]]; then
            echo "nothing done"
            exit 1
        fi
        remote "sudo systemctl stop $SERVICE && rm -f $DB* && mv $LOG SportsArb/data/record_old.log; sudo systemctl start $SERVICE"
        ;;
    shell)
        remote
        ;;
    allow-ip)
        aws_ec2 authorize-security-group-ingress --group-id "$SPORTSARB_SECURITY_GROUP" --protocol tcp --port 22 \
            --cidr "$(curl -s https://checkip.amazonaws.com)/32"
        ;;
    instance)
        aws_ec2 describe-instances --instance-ids "$SPORTSARB_INSTANCE" \
            --query "Reservations[].Instances[].[State.Name,PublicIpAddress]" --output text
        ;;
    cpu)
        aws cloudwatch get-metric-statistics --region "$SPORTSARB_REGION" --namespace AWS/EC2 --metric-name CPUUtilization \
            --dimensions "Name=InstanceId,Value=$SPORTSARB_INSTANCE" --start-time "$(one_hour_ago)" \
            --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Average Maximum \
            --query "sort_by(Datapoints,&Timestamp)[].[Timestamp,Average,Maximum]" --output text
        ;;
    disk)
        remote "df -h / | tail -1; ls -la --block-size=M $DB"
        ;;
    stop-instance)
        aws_ec2 stop-instances --instance-ids "$SPORTSARB_INSTANCE"
        ;;
    start-instance)
        aws_ec2 start-instances --instance-ids "$SPORTSARB_INSTANCE"
        echo "The public IP changes on start. Check it with: scripts/ops.sh instance"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "unknown command: $command" >&2
        usage >&2
        exit 1
        ;;
esac
