#include "firewall.h"

#include <netfw.h>
#include <shellapi.h>

#include <string>

namespace lfrd {
namespace {

std::wstring ExePath() {
  wchar_t path[MAX_PATH];
  GetModuleFileNameW(nullptr, path, MAX_PATH);
  return path;
}

template <class T>
void Release(T*& p) {
  if (p) p->Release();
  p = nullptr;
}

}  // namespace

bool FirewallNeedsFix() {
  INetFwPolicy2* policy = nullptr;
  if (FAILED(CoCreateInstance(__uuidof(NetFwPolicy2), nullptr, CLSCTX_INPROC_SERVER, __uuidof(INetFwPolicy2),
                              reinterpret_cast<void**>(&policy)))) {
    return false;  // can't tell; don't nag
  }
  // Public and Private both, whichever this network is now: the Wi-Fi can be switched later.
  long needed = 0;
  for (long bit : {NET_FW_PROFILE2_PRIVATE, NET_FW_PROFILE2_PUBLIC}) {
    VARIANT_BOOL on = VARIANT_FALSE;
    if (SUCCEEDED(policy->get_FirewallEnabled(static_cast<NET_FW_PROFILE_TYPE2>(bit), &on)) && on == VARIANT_TRUE) {
      needed |= bit;
    }
  }
  if (!needed) {
    Release(policy);
    return false;
  }

  const std::wstring exe = ExePath();
  long allowed = 0;
  bool blocked = false;
  INetFwRules* rules = nullptr;
  IUnknown* enumUnk = nullptr;
  IEnumVARIANT* it = nullptr;
  if (SUCCEEDED(policy->get_Rules(&rules)) && SUCCEEDED(rules->get__NewEnum(&enumUnk)) &&
      SUCCEEDED(enumUnk->QueryInterface(IID_PPV_ARGS(&it)))) {
    VARIANT v;
    VariantInit(&v);
    while (it->Next(1, &v, nullptr) == S_OK) {
      INetFwRule* rule = nullptr;
      if (v.vt == VT_DISPATCH && v.pdispVal && SUCCEEDED(v.pdispVal->QueryInterface(IID_PPV_ARGS(&rule)))) {
        BSTR app = nullptr;
        NET_FW_RULE_DIRECTION dir;
        VARIANT_BOOL enabled;
        NET_FW_ACTION action;
        long profiles = 0;
        if (SUCCEEDED(rule->get_ApplicationName(&app)) && app && _wcsicmp(app, exe.c_str()) == 0 &&
            SUCCEEDED(rule->get_Direction(&dir)) && dir == NET_FW_RULE_DIR_IN &&
            SUCCEEDED(rule->get_Enabled(&enabled)) && enabled == VARIANT_TRUE &&
            SUCCEEDED(rule->get_Action(&action)) && SUCCEEDED(rule->get_Profiles(&profiles))) {
          if (action == NET_FW_ACTION_ALLOW) {
            allowed |= profiles;
          } else if (profiles & needed) {
            blocked = true;  // block rules win over allow rules
          }
        }
        SysFreeString(app);
        Release(rule);
      }
      VariantClear(&v);
    }
  }
  Release(it);
  Release(enumUnk);
  Release(rules);
  Release(policy);
  return blocked || (allowed & needed) != needed;
}

bool FixFirewall(HWND owner) {
  const std::wstring exe = ExePath();
  const std::wstring args =
      L"/c netsh advfirewall firewall delete rule name=all program=\"" + exe +
      L"\" & netsh advfirewall firewall add rule name=\"Triton Screen Share\" dir=in action=allow "
      L"program=\"" + exe + L"\" enable=yes profile=any";
  SHELLEXECUTEINFOW sei{sizeof(sei)};
  sei.fMask = SEE_MASK_NOCLOSEPROCESS;
  sei.hwnd = owner;
  sei.lpVerb = L"runas";
  sei.lpFile = L"cmd.exe";
  sei.lpParameters = args.c_str();
  sei.nShow = SW_HIDE;
  if (!ShellExecuteExW(&sei) || !sei.hProcess) return false;
  WaitForSingleObject(sei.hProcess, 30000);
  DWORD code = 1;
  GetExitCodeProcess(sei.hProcess, &code);
  CloseHandle(sei.hProcess);
  return code == 0;
}

}  // namespace lfrd
