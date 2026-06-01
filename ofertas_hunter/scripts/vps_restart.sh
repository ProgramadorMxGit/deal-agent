#!/usr/bin/env bash
sudo systemctl restart ofertas-hunter
sleep 3
sudo systemctl status ofertas-hunter --no-pager | head -10
