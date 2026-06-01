#!/usr/bin/env bash
sudo systemctl stop ofertas-hunter
sudo systemctl status ofertas-hunter --no-pager | head -5
