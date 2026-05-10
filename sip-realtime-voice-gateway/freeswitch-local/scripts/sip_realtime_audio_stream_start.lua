local uuid = session:getVariable("uuid")
local call_id = session:getVariable("sip_realtime_gateway_call_id")
local gateway_base_url = session:getVariable("sip_realtime_gateway_base_url")

if call_id == nil or call_id == "" then
  call_id = uuid
end

if gateway_base_url == nil or gateway_base_url == "" then
  gateway_base_url = "ws://host.docker.internal:9101/media/fs/"
end

local ws_url = gateway_base_url .. call_id
local api = freeswitch.API()
local command = uuid .. " start " .. ws_url .. " mono 8k"

freeswitch.consoleLog(
  "INFO",
  "SIP realtime media stream start uuid=" .. uuid ..
    " call_id=" .. call_id ..
    " ws_url=" .. ws_url .. "\n"
)

local result = api:execute("uuid_audio_stream", command)

freeswitch.consoleLog(
  "INFO",
  "SIP realtime media stream result uuid=" .. uuid ..
    " result=" .. tostring(result) .. "\n"
)
