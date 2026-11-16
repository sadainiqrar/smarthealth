#!/usr/bin/env bash
# scripts/aws_stop.sh -- stop the server, then the NAT instance. Data stays on the disk.
. "$(dirname "$0")/aws_lib.sh"
"$AWS" ec2 stop-instances --instance-ids "$(tf_output server_instance_id)" >/dev/null
"$AWS" ec2 stop-instances --instance-ids "$(tf_output nat_instance_id)" >/dev/null
echo "stopping (disks are kept; about \$4.60 a month while stopped)"
