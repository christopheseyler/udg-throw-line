## Example autoload talking to udg-throw-line-service (Godot 4).
##
## Register it in Project Settings > Autoload, e.g. as "ThrowLine", then:
##     ThrowLine.line_crossed.connect(_on_foot_fault)
##     ThrowLine.laser(true)                    # laser line on for the game
##     if ThrowLine.is_crossed(): ...           # somebody is on the line now
##     var reply := await ThrowLine.calibrate() # setup screen, nobody on the line
##     var image := await ThrowLine.snapshot()  # what the camera sees
##
## Everything goes over connectionless UDP to 127.0.0.1:47810. The autoload
## subscribes to the events at startup and renews the subscription every few
## seconds, so it survives a restart of the service. When the service is not
## running (dev PC), nothing happens and nothing blocks; run it on the PC with
## `make simulate` to get real events. Protocol: doc/PROTOCOLE.md.
extends Node

const HOST := "127.0.0.1"
const PORT := 47810
## Renewal period of the subscription (the service keeps it 10 s).
const RENEW_SECONDS := 3.0

## Line state changed: "clear", "crossed", "blocked" or "unknown" (not watched:
## laser off, not calibrated, camera missing, calibrating). `info` holds the
## event fields: pos/from/to (0-1 along the line) and runs for "crossed",
## coverage for "blocked", duration for "clear", reason for "unknown".
signal line_changed(state: String, info: Dictionary)
## Shortcuts of line_changed.
signal line_crossed(info: Dictionary)
signal line_cleared(info: Dictionary)
## Service state changed: {"laser", "camera", "calibrated", "watching"}.
signal service_changed(status: Dictionary)
## Every "err ..." reply, so mistakes show up while developing the game.
signal service_error(message: String)

## Last known line state and service state.
var line_state := "unknown"
var line_info := {}
var service_status := {}

var _udp := PacketPeerUDP.new()
var _renew_left := 0.0
var _replies := {}      # command keyword -> last reply


func _ready() -> void:
	# Bind to an ephemeral loopback port so replies and events can come back.
	# Never connect_to_host(): a connected UDP socket turns the ICMP "port
	# unreachable" of an absent service into errors on the next calls.
	var err := _udp.bind(0, "127.0.0.1")
	if err != OK:
		push_warning("ThrowLine: cannot bind a UDP socket (%s)" % error_string(err))
	_udp.set_dest_address(HOST, PORT)


func _process(delta: float) -> void:
	_renew_left -= delta
	if _renew_left <= 0.0:
		_renew_left = RENEW_SECONDS
		_send_text("subscribe")
	while _udp.get_available_packet_count() > 0:
		_handle(_udp.get_packet().get_string_from_ascii().strip_edges())


## True while somebody's foot is across the line.
func is_crossed() -> bool:
	return line_state == "crossed"


## True when the line is watched (laser on, calibrated, camera running).
func is_watching() -> bool:
	return line_state != "unknown"


## Laser line on/off. The line is only watched while the laser is on.
func laser(on: bool) -> void:
	_send_text("laser on" if on else "laser off")


## Finds the laser line and records it; nobody must stand on the line. Takes
## about a second. Returns "ok calibrate line=... usable=..." or
## "err calibrate <why>" ("" if the service did not answer).
## `line` (optional): [x0, y0, x1, y1] normalized, to impose the line.
func calibrate(line := []) -> String:
	var command := "calibrate"
	if line.size() == 4:
		command += " line=%f,%f,%f,%f" % [line[0], line[1], line[2], line[3]]
	return await _request(command, "calibrate", 15000)


## The current camera frame (half resolution, color, line samples drawn:
## green = intact, yellow = broken, magenta = not watched), or null.
func snapshot() -> Image:
	var reply := await _request("snapshot", "snapshot", 5000)
	var path := _field(reply, "path")
	if not reply.begins_with("ok") or path == "":
		return null
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		push_warning("ThrowLine: cannot read %s" % path)
		return null
	var image := Image.new()
	if image.load_bmp_from_buffer(file.get_buffer(file.get_length())) != OK:
		return null
	return image


## Service version and state, to be awaited:
##     var reply := await ThrowLine.ping()
## "pong <version> api=<n> laser=... camera=... calibrated=... watching=...",
## or "" when the service did not answer within timeout_ms.
func ping(timeout_ms := 500) -> String:
	return await _request("ping", "ping", timeout_ms)


## Full status as a Dictionary (empty when the service did not answer).
func status(timeout_ms := 500) -> Dictionary:
	var reply := await _request("status", "status", timeout_ms)
	return _fields(reply, 2) if reply.begins_with("ok") else {}


func _request(command: String, keyword: String, timeout_ms: int) -> String:
	_replies.erase(keyword)
	_send_text(command)
	var deadline := Time.get_ticks_msec() + timeout_ms
	while not _replies.has(keyword) and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	return _replies.get(keyword, "")


func _handle(text: String) -> void:
	var words := text.split(" ", false)
	if words.is_empty():
		return
	match words[0]:
		"event":
			if words.size() >= 2 and words[1] == "line":
				_on_line(_fields(text, 2))
			elif words.size() >= 2 and words[1] == "service":
				service_status = _fields(text, 2)
				service_changed.emit(service_status)
		"pong":
			_replies["ping"] = text
		"ok", "err":
			# Every reply repeats its command keyword: "ok laser on",
			# "err calibrate no laser line found ...".
			if words.size() >= 2:
				_replies[words[1]] = text
			if words[0] == "err":
				push_warning("ThrowLine: " + text)
				service_error.emit(text)


func _on_line(info: Dictionary) -> void:
	var state: String = info.get("state", "unknown")
	for key in ["pos", "from", "to", "coverage", "duration"]:
		if info.has(key):
			info[key] = float(info[key])
	if info.has("runs"):
		info["runs"] = int(info["runs"])
	var changed := state != line_state
	line_state = state
	line_info = info
	if not changed:
		return
	line_changed.emit(state, info)
	if state == "crossed":
		line_crossed.emit(info)
	elif state == "clear":
		line_cleared.emit(info)


## "word word key=value key=value" -> {key: value}, skipping `skip` words.
static func _fields(text: String, skip: int) -> Dictionary:
	var result := {}
	var words := text.split(" ", false)
	for i in range(skip, words.size()):
		var parts := words[i].split("=", true, 1)
		if parts.size() == 2:
			result[parts[0]] = parts[1]
	return result


static func _field(text: String, key: String) -> String:
	return _fields(text, 0).get(key, "")


func _send_text(text: String) -> void:
	_udp.put_packet(text.to_ascii_buffer())
