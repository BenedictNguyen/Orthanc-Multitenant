-- ================================
-- Gán tenant khi nhận instance
-- ================================
local tenants = {
  ["ORTHANC_WL"]  = "WL",
  ["ORTHANC_XR"]  = "XR",
  ["ORTHANC_BDM"] = "BDM",
  ["ORTHANC_US"]  = "US",
  ["ORTHANC_CT"]  = "CT",
  ["ORTHANC_MR"]  = "MR",
  ["ORTHANC_XA"]  = "XA"
}

function OnReceivedInstance(instanceId, source, info)
  local tenantName = tenants[info["CalledAet"]]

  if tenantName then
    print("[Lua] Instance " .. instanceId .. " received for tenant: " .. tenantName)
    RestApiPost("/instances/" .. instanceId .. "/metadata/Tenant", tenantName)
  else
    print("[Lua] Instance " .. instanceId .. " received with no tenant (CalledAet=" .. tostring(info["CalledAet"]) .. ")")
  end
end


-- ================================
-- Auto-forward khi series stable
-- ================================
function OnStableSeries(seriesId, tags, metadata)
  local modality = tags["Modality"] or "UNKNOWN"

  -- thử lấy Tenant từ metadata
  local tenantName = nil
  local seriesInfo = ParseJson(RestApiGet("/series/" .. seriesId))
  if seriesInfo and seriesInfo.Instances and #seriesInfo.Instances > 0 then
    local instanceId = seriesInfo.Instances[1]
    tenantName = ParseJson(RestApiGet("/instances/" .. instanceId .. "/metadata/Tenant"))
  end

  if tenantName then
    print(string.format("[Lua] Series %s stable for tenant '%s' → forwarding", seriesId, tenantName))
  else
    print(string.format("[Lua] Series %s stable with no tenant → fallback using Modality=%s", seriesId, modality))
    tenantName = modality
  end

  -- retry cơ chế forward
  local max_retry = 3
  local wait_sec = 5
  local attempt = 0
  local success = false

  while (max_retry == 0 or attempt < max_retry) and not success do
    local error_message = nil

    local ok, err = pcall(function()
      local body = DumpJson({ seriesId })
      local resp = RestApiPost("/modalities/DATACENTER_PACS/store", body)

      if resp == nil then
        error_message = "No response from PACS"
        error(error_message)
      end

      local data = ParseJson(resp)
      if data and data["Error"] then
        error_message = data["Error"]
        error(error_message)
      end
    end)

    if ok then
      print("[Lua] Successfully forwarded series " .. seriesId .. " (" .. tenantName .. ")")
      success = true
    else
      if not error_message then
        error_message = tostring(err)
      end
      print("[Lua] Attempt " .. attempt .. " failed: " .. error_message .. " → retrying in " .. wait_sec .. "s")
      os.execute("sleep " .. wait_sec)
      attempt = attempt + 1
    end
  end

  if not success then
    print("[Lua] Series " .. seriesId .. " failed after " .. attempt .. " retries → queued for later")
  end
end

print("[Lua] Multi-tenant forward script initialized")
