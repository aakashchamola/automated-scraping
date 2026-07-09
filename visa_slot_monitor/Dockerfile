# Visa slot monitor — for always-on boxes (VPS / Raspberry Pi / NAS).
# The siren is useless inside a container; phone push (ntfy) is the alarm path.
#
#   docker build -t visa-slot-monitor .
#   docker run -it --name vsm -v vsm_state:/app/logs -v $PWD/config.json:/app/config.json \
#       -v $PWD/visa_monitor.session:/app/visa_monitor.session visa-slot-monitor
#
# First run needs -it for the Telegram OTP prompt; afterwards restart with -d.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "run_forever.py"]
