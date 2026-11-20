# Saved by Bram from the agents' `ffmpeg` runs. Replaces:
#   ffmpeg -hide_banner -nostats -i ~/Desktop/edit-1-leveled-denoised.mp4 -vn -af ebur128=peak=true -f null - 2>&1 | grep -A14 Summary | grep -E "I:|LRA:|Peak:"  ->  make audio-loudness F=~/Desktop/edit-1-leveled-denoised.mp4
#   ffmpeg -nostats -i $f -vn -af ebur128=peak=true -f null - 2>&1 | awk '/Summary/{s=1} s && /I:/{i=$2} s && /LRA:/{l=$2} s && /Peak:/{p=$2} END{printf "I %s LUFS LRA %s LU true peak %s dBTP\n", i, l, p}'  ->  make audio-loudness F=$f
#   ffmpeg -hide_banner -i "$f" -af "loudnorm=print_format=summary" -f null - 2>&1 | grep -E "Input Integrated|Input True Peak"  ->  make audio-loudness F="$f"
#   c=$(ffmpeg -ss $2 -t $3 -i "$1" -vn -af "pan=mono|c0=c0,highpass=f=8000,highpass=f=8000,astats" -f null - 2>&1 | grep 'RMS level dB' | tail -1 | awk '{print $NF}')  ->  make audio-segment-rms F="$1" SS=$2 T=$3 AF='pan=mono|c0=c0,highpass=f=8000,highpass=f=8000'
#   ffmpeg -hide_banner -nostats -ss 12 -t 8 -i ~/Desktop/edit-1.mp4 -vn -af "$af,astats=measure_perchannel=none:measure_overall=RMS_level" -f null - 2>&1 | grep "RMS level" | sed 's/.*: //' | tr '\n' ' '  ->  make audio-segment-rms F=~/Desktop/edit-1.mp4 SS=12 T=8 AF="$af"
#   ffmpeg -hide_banner -nostats -ss 24 -t 8 -i ~/Desktop/edit-1.mp4 -vn -af "$af,astats=measure_perchannel=none:measure_overall=RMS_level" -f null - 2>&1 | grep "RMS level" | sed 's/.*: //'  ->  make audio-segment-rms F=~/Desktop/edit-1.mp4 SS=24 T=8 AF="$af"
#   ffmpeg -v info -i $f -af silencedetect=n=-40dB:d=0.7 -f null - 2>&1 | grep -o "silence_\(start\|end\): [0-9.]*" | paste - - | awk '{printf "%.2f-%.2f ", $2,$4} END{print "<- gaps over 0.7 s"}'  ->  make audio-gaps F=$f
# Not a task: ffmpeg -hide_banner -v error -y -i ~/Desktop/edit-1.mp4 -i hp.wav … -af "asendcmd=f=ride-hp.cmd,…" … ~/Desktop/edit-1-leveled-denoised.mp4 (a one-off render from that session's own inputs)
.PHONY: audio-loudness audio-segment-rms audio-gaps
comma := ,
NOISE ?= -40
MIN ?= 0.7

# Usage: make audio-loudness F=<file>
# Integrated loudness, loudness range and true peak, on one line.
audio-loudness:
	@ffmpeg -hide_banner -nostats -i "$(F)" -vn -af ebur128=peak=true -f null - 2>&1 | awk '/Summary/{s=1} s && /I:/{i=$$2} s && /LRA:/{l=$$2} s && /Peak:/{p=$$2} END{printf "I %s LUFS  LRA %s LU  true peak %s dBTP\n", i, l, p}'

# Usage: make audio-segment-rms F=<file> SS=<start s> T=<length s> [AF=<filter chain>]
# The RMS level (dB) of a stretch, after an optional filter chain.
audio-segment-rms:
	@ffmpeg -hide_banner -nostats -ss $(SS) -t $(T) -i "$(F)" -vn -af "$(if $(AF),$(AF)$(comma),)astats=measure_perchannel=none:measure_overall=RMS_level" -f null - 2>&1 | grep "RMS level" | tail -1 | sed 's/.*: //'

# Usage: make audio-gaps F=<file> [NOISE=-40] [MIN=0.7]
# Quiet stretches below NOISE dB lasting MIN s or more, as start-end pairs.
audio-gaps:
	@ffmpeg -v info -i "$(F)" -af silencedetect=n=$(NOISE)dB:d=$(MIN) -f null - 2>&1 | grep -o "silence_\(start\|end\): [0-9.]*" | paste - - | awk '{printf "%.2f-%.2f ", $$2, $$4} END{print "<- gaps over $(MIN) s"}'
