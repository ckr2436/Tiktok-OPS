package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWaitProfileCDPUsesDevToolsActivePort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/json/version" {
			http.NotFound(response, request)
			return
		}
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"Browser":"Chrome/test"}`))
	})}
	go server.Serve(listener)
	t.Cleanup(func() { _ = server.Close() })

	profile := t.TempDir()
	port := listener.Addr().(*net.TCPAddr).Port
	contents := fmt.Sprintf("%d\n/devtools/browser/test\n", port)
	if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte(contents), 0600); err != nil {
		t.Fatal(err)
	}

	if actual := waitProfileCDP(profile, 2*time.Second); actual != port {
		t.Fatalf("expected CDP port %d, got %d", port, actual)
	}
}

func TestDesiredSlotCarriesBoundedYtDlpCookieRules(t *testing.T) {
	spec := DesiredSlot{
		Purpose:         "yt_dlp_account",
		CookiePageHosts: []string{"instagram.com"},
		CookieDomains:   []string{"instagram.com"},
		CookieNames:     []string{"sessionid"},
	}
	encoded, err := json.Marshal(spec)
	if err != nil {
		t.Fatal(err)
	}
	var decoded DesiredSlot
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Purpose != "yt_dlp_account" || len(decoded.CookieNames) != 1 || decoded.CookieNames[0] != "sessionid" {
		t.Fatalf("yt-dlp cookie capture rules were not preserved: %#v", decoded)
	}
	changed := spec
	changed.CookieNames = []string{"different_session"}
	if sameSlot(spec, changed) {
		t.Fatal("changed cookie allowlist must restart the exact slot probe")
	}
}

func TestWaitProfileCDPRejectsStalePortFile(t *testing.T) {
	profile := t.TempDir()
	if err := os.WriteFile(filepath.Join(profile, "DevToolsActivePort"), []byte("1\n/stale\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if actual := waitProfileCDP(profile, 20*time.Millisecond); actual != 0 {
		t.Fatalf("expected no CDP port, got %d", actual)
	}
}

func TestDormantDesiredSlotDoesNotStartChrome(t *testing.T) {
	a := &agent{
		slots:             make(map[string]*slotRuntime),
		dormant:           make(map[string]DesiredSlot),
		serverRestartSeen: make(map[string]bool),
		retries:           make(map[string]slotRetryState),
	}
	a.reconcile([]DesiredSlot{{
		BridgeID: "br_api_wait", Desired: true, LocalPort: 9222,
		ServerPort: 9322, SSHHost: "host", SSHUser: "user", SSHPort: 22,
		Mode: "dormant",
	}})

	if len(a.slots) != 0 {
		t.Fatalf("dormant slot must not start Chrome, got %d running slots", len(a.slots))
	}
	if _, ok := a.dormant["br_api_wait"]; !ok {
		t.Fatal("dormant slot was not retained for heartbeat acknowledgement")
	}
	statuses := a.statuses()
	if len(statuses) != 1 || statuses[0].Mode != "dormant" || statuses[0].Connected {
		t.Fatalf("unexpected dormant heartbeat: %#v", statuses)
	}
}

func TestRequiresAgentUpdateOnlyForDifferentServerVersion(t *testing.T) {
	if !requiresAgentUpdate("2026.07.18.1", "2026.07.18.2", true) {
		t.Fatal("different requested server version must update")
	}
	if requiresAgentUpdate("2026.07.18.2", "2026.07.18.2", true) {
		t.Fatal("matching version must not reinstall itself")
	}
	if requiresAgentUpdate("2026.07.18.1", "2026.07.18.2", false) {
		t.Fatal("server must explicitly request an update")
	}
}

func TestBindingIdentitySeparatesWorkspaceUsersOnOneWindowsDevice(t *testing.T) {
	first := Config{WorkspaceID: 1, UserID: 10, DeviceID: "windows-a", RuntimePortBase: 20000}
	second := Config{WorkspaceID: 2, UserID: 20, DeviceID: "windows-a", RuntimePortBase: 20256}
	if bindingIdentity(first) == bindingIdentity(second) {
		t.Fatal("separate workspace/user bindings must not overwrite one another")
	}
	firstAgent := &agent{config: first}
	secondAgent := &agent{config: second}
	if firstAgent.runtimeDebugPort(9230) == secondAgent.runtimeDebugPort(9230) {
		t.Fatal("separate bindings must not share a Doubao/Chrome debugging port")
	}
}

func TestHostIdentityIsStableAcrossLogicalBindings(t *testing.T) {
	root := t.TempDir()
	first, err := loadOrCreateHostID(root)
	if err != nil {
		t.Fatal(err)
	}
	second, err := loadOrCreateHostID(root)
	if err != nil {
		t.Fatal(err)
	}
	if first != second || len(first) != 32 {
		t.Fatalf("physical host identity must be stable, got %q and %q", first, second)
	}
}

func TestBindingConfigsRemainAdditiveAndUseSeparatePortBlocks(t *testing.T) {
	root := t.TempDir()
	first := Config{APIURL: "https://example.test/one", Token: "one", WorkspaceID: 1, UserID: 10, DeviceID: "flow"}
	second := Config{APIURL: "https://example.test/two", Token: "two", WorkspaceID: 2, UserID: 20, DeviceID: "content"}
	for _, config := range []*Config{&first, &second} {
		if err := assignRuntimePortBase(root, config); err != nil {
			t.Fatal(err)
		}
		bindingRoot := bridgeBindingRoot(root, *config)
		if err := os.MkdirAll(bindingRoot, 0700); err != nil {
			t.Fatal(err)
		}
		if err := writeConfig(filepath.Join(bindingRoot, "agent.json"), *config); err != nil {
			t.Fatal(err)
		}
	}
	if first.RuntimePortBase == second.RuntimePortBase {
		t.Fatal("logical bindings on one host must have separate runtime port blocks")
	}
	oldLocalAppData := os.Getenv("LOCALAPPDATA")
	t.Cleanup(func() { _ = os.Setenv("LOCALAPPDATA", oldLocalAppData) })
	_ = os.Setenv("LOCALAPPDATA", filepath.Dir(filepath.Dir(root)))
}

func TestLegacyBindingMigrationKeepsHostBrowserProfilesInPlace(t *testing.T) {
	root := t.TempDir()
	config := Config{
		APIURL: "https://example.test", Token: "secret", WorkspaceID: 1, UserID: 1,
		DeviceID: "flowdev::slot:0", RuntimePortBase: 20000,
	}
	if err := writeConfig(filepath.Join(root, "agent.json"), config); err != nil {
		t.Fatal(err)
	}
	profile := filepath.Join(root, "profiles", "slot-9223", "Default", "Network")
	if err := os.MkdirAll(profile, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(profile, "Cookies"), []byte("persisted-login"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := migrateLegacyBinding(root); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(profile, "Cookies")); err != nil {
		t.Fatalf("host Profile store must not move into one logical binding: %v", err)
	}
	if _, err := os.Stat(filepath.Join(bridgeBindingRoot(root, config), "agent.json")); err != nil {
		t.Fatalf("legacy binding config was not migrated: %v", err)
	}
}

func TestAffectedBindingProfileIsRestoredToHostSlot(t *testing.T) {
	installRoot := t.TempDir()
	bindingRoot := filepath.Join(installRoot, "bindings", "affected-binding")
	source := filepath.Join(bindingRoot, "profiles", "slot-9223", "Default", "Network")
	if err := os.MkdirAll(source, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(source, "Cookies"), []byte("persisted-flow-login"), 0600); err != nil {
		t.Fatal(err)
	}
	a := &agent{root: bindingRoot, profileRoot: installRoot}
	spec := DesiredSlot{BridgeID: "flow-account", Purpose: "flow_account", LocalPort: 9223}
	profile, recoveredFrom, err := a.recoverHostBrowserProfile(spec, "profiles", 20031)
	if err != nil {
		t.Fatal(err)
	}
	if recoveredFrom == "" {
		t.Fatal("affected binding-local Profile was not detected")
	}
	expected := filepath.Join(installRoot, "profiles", "slot-9223")
	if !samePath(profile, expected) {
		t.Fatalf("expected host Profile %s, got %s", expected, profile)
	}
	if data, err := os.ReadFile(filepath.Join(profile, "Default", "Network", "Cookies")); err != nil || string(data) != "persisted-flow-login" {
		t.Fatalf("persisted Flow login was not restored: %q %v", data, err)
	}
	if _, err := os.Stat(filepath.Join(bindingRoot, "profiles", "slot-9223")); !os.IsNotExist(err) {
		t.Fatalf("binding-local slot directory should have moved atomically, got %v", err)
	}
}

func TestAutomaticFlowWorkRequiresExistingNonEmptyProfile(t *testing.T) {
	automatic := DesiredSlot{Purpose: "flow_account", LoginOnly: true, AutomaticVisit: true}
	if !flowProfileRequiresExistingState(automatic) {
		t.Fatal("automatic Flow maintenance must not create an empty Profile")
	}
	capture := DesiredSlot{Purpose: "flow_account", CaptureRequired: true}
	if !flowProfileRequiresExistingState(capture) {
		t.Fatal("Flow capture must not create an empty Profile")
	}
	manual := DesiredSlot{Purpose: "flow_account", LoginOnly: true, Interactive: true}
	if flowProfileRequiresExistingState(manual) {
		t.Fatal("explicit first-login window must be allowed to create its Profile")
	}
}

func TestHeartbeatReportsAutomaticUpdateFailureForManualFallback(t *testing.T) {
	payload := Heartbeat{UpdateState: "failed", UpdateError: "download interrupted"}
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	text := string(encoded)
	if !strings.Contains(text, `"update_state":"failed"`) || !strings.Contains(text, `"update_error":"download interrupted"`) {
		t.Fatalf("update status missing from heartbeat: %s", text)
	}
}

func TestFlowCaptureChangeRestartsOnlyTheSamePurposeProfile(t *testing.T) {
	base := DesiredSlot{
		BridgeID: "flow-1", LocalPort: 9224, ServerPort: 9324,
		SSHHost: "host", SSHUser: "user", SSHPort: 22,
		Purpose: "flow_account", TargetURL: "https://labs.google/fx/tools/flow", CaptureID: "capture-a",
	}
	same := base
	if !sameSlot(base, same) {
		t.Fatal("identical Flow slot must retain its Chrome profile")
	}
	changed := base
	changed.CaptureID = "capture-b"
	if sameSlot(base, changed) {
		t.Fatal("new capture cycle must restart the exact profile before collection")
	}
	login := base
	login.LoginOnly = true
	if sameSlot(base, login) {
		t.Fatal("normal login mode must restart the same profile before CDP capture")
	}
	automatic := login
	automatic.AutomaticVisit = true
	if sameSlot(login, automatic) {
		t.Fatal("automatic normal-Profile visit must restart the exact login runtime")
	}
	proxyChanged := base
	proxyChanged.ProxyURL = "socks5h://192.168.1.22:7893"
	if sameSlot(base, proxyChanged) {
		t.Fatal("proxy changes must restart the exact account profile")
	}
	content := base
	content.Purpose = "content_factory"
	if sameSlot(base, content) {
		t.Fatal("Flow account slots must never be reused as content project slots")
	}
}

func TestNewCaptureCycleDoesNotInheritOldLocalBackoff(t *testing.T) {
	a := &agent{
		retries:         make(map[string]slotRetryState),
		retryCaptureIDs: make(map[string]string),
	}
	spec := DesiredSlot{BridgeID: "flow-1", Purpose: "flow_account", CaptureID: "capture-a"}
	a.retries[spec.BridgeID] = slotRetryState{Failures: 5, NextAt: time.Now().Add(15 * time.Minute)}
	a.resetSlotRetryForCapture(spec)
	if _, ok := a.retries[spec.BridgeID]; ok {
		t.Fatal("new capture cycle must clear an older browser backoff")
	}

	a.retries[spec.BridgeID] = slotRetryState{Failures: 1, NextAt: time.Now().Add(time.Minute)}
	a.resetSlotRetryForCapture(spec)
	if _, ok := a.retries[spec.BridgeID]; !ok {
		t.Fatal("same capture cycle must preserve its bounded retry backoff")
	}

	spec.CaptureID = "capture-b"
	a.resetSlotRetryForCapture(spec)
	if _, ok := a.retries[spec.BridgeID]; ok {
		t.Fatal("replacement capture cycle must clear the prior cycle backoff")
	}
}

func TestChromeProxyArgumentSupportsMixedProxyListener(t *testing.T) {
	if got := chromeProxyArgument("socks5h://192.168.1.21:7893"); got != "--proxy-server=socks5://192.168.1.21:7893" {
		t.Fatalf("unexpected SOCKS proxy argument: %s", got)
	}
	if got := chromeProxyArgument("http://192.168.1.21:7893"); got != "--proxy-server=http://192.168.1.21:7893" {
		t.Fatalf("unexpected HTTP proxy argument: %s", got)
	}
}

func TestAutomaticFlowVisitUsesRendererWithoutBackgroundThrottling(t *testing.T) {
	args := automaticFlowVisitArguments()
	joined := strings.Join(args, " ")
	for _, required := range []string{
		"--start-minimized",
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
	} {
		if !strings.Contains(joined, required) {
			t.Fatalf("automatic Flow visit is missing %s", required)
		}
	}
}

func TestSlotStatusSerializesSafeFlowDiagnostics(t *testing.T) {
	payload, err := json.Marshal(SlotStatus{
		BridgeID:   "flow-account",
		FlowStatus: "login_required",
		SessionDiagnostics: map[string]any{
			"candidate_count":       float64(0),
			"document_cookie_names": []string{"consent"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(payload), `"session_diagnostics"`) {
		t.Fatalf("heartbeat omitted Flow session diagnostics: %s", payload)
	}
}

func TestDoubaoProviderUsesMinimizedNonHeadlessChrome(t *testing.T) {
	doubao := DesiredSlot{Purpose: "doubao_lab", ProviderRequest: true}
	args := browserPresentationArguments(doubao)
	joined := strings.Join(args, " ")
	if strings.Contains(joined, "headless") {
		t.Fatalf("Doubao provider runtime must not be headless: %s", joined)
	}
	if !strings.Contains(joined, "--start-minimized") {
		t.Fatalf("Doubao provider runtime should not steal focus: %s", joined)
	}

	manualCapture := DesiredSlot{Purpose: "doubao_lab", ProviderRequest: true, Interactive: true}
	manualArgs := strings.Join(browserPresentationArguments(manualCapture), " ")
	if strings.Contains(manualArgs, "--start-minimized") || strings.Contains(manualArgs, "headless") {
		t.Fatalf("manual capture must be visible on the interactive desktop: %s", manualArgs)
	}
	if !strings.Contains(manualArgs, "--window-position=80,60") {
		t.Fatalf("manual capture should open on the primary desktop: %s", manualArgs)
	}

	flow := DesiredSlot{Purpose: "flow_account", CaptureRequired: true}
	if !strings.Contains(strings.Join(browserPresentationArguments(flow), " "), "--headless=new") {
		t.Fatal("Flow capture runtime should remain headless")
	}
	ytDlp := DesiredSlot{Purpose: "yt_dlp_account", CaptureRequired: true}
	if !strings.Contains(strings.Join(browserPresentationArguments(ytDlp), " "), "--headless=new") {
		t.Fatal("automatic yt-dlp Cookie capture must remain headless")
	}
	ytDlpLogin := DesiredSlot{Purpose: "yt_dlp_account", LoginOnly: true}
	if strings.Contains(strings.Join(browserPresentationArguments(ytDlpLogin), " "), "headless") {
		t.Fatal("explicit yt-dlp login must remain visible")
	}
}

func TestDoubaoDesktopRuntimeIsExplicitAndProfileScoped(t *testing.T) {
	chrome := DesiredSlot{Purpose: "doubao_lab", Runtime: "chrome", LocalPort: 9230}
	desktop := chrome
	desktop.Runtime = "doubao_desktop"
	if isDoubaoDesktopRuntime(chrome) {
		t.Fatal("Chrome remains the default Doubao runtime")
	}
	if !isDoubaoDesktopRuntime(desktop) {
		t.Fatal("explicit Doubao desktop runtime was not recognized")
	}
	if sameSlot(chrome, desktop) {
		t.Fatal("changing runtime must restart only the exact account slot")
	}
	args := strings.Join(doubaoDesktopArguments(desktop, `C:\\Profiles\\slot-9230`), " ")
	for _, expected := range []string{
		"--remote-debugging-port=9230",
		`--user-data-dir=C:\\Profiles\\slot-9230`,
	} {
		if !strings.Contains(args, expected) {
			t.Fatalf("missing desktop isolation argument %s in %s", expected, args)
		}
	}
}

func TestJimengLabKeepsItsOwnPurposeScopedProfile(t *testing.T) {
	jimeng := DesiredSlot{
		BridgeID: "jimeng-1", LocalPort: 9225, ServerPort: 9325,
		SSHHost: "host", SSHUser: "user", SSHPort: 22,
		Purpose: "jimeng_lab", TargetURL: "https://jimeng.jianying.com/ai-tool/generate?type=video",
		CaptureID: "capture-a", LoginOnly: true,
	}
	if !sameSlot(jimeng, jimeng) {
		t.Fatal("identical JiMeng slot must retain its fixed Chrome profile")
	}
	flow := jimeng
	flow.Purpose = "flow_account"
	if sameSlot(jimeng, flow) {
		t.Fatal("JiMeng and Flow accounts must never share a browser runtime")
	}
	changed := jimeng
	changed.CaptureID = "capture-b"
	if sameSlot(jimeng, changed) {
		t.Fatal("new JiMeng capture cycle must restart only its exact profile")
	}
}

func TestJimengCookieDomainsAcceptOnlyExpectedParents(t *testing.T) {
	allowed := []string{"jianying.com", "capcut.com"}
	for _, domain := range []string{".jianying.com", "jimeng.jianying.com", ".capcut.com", "dreamina.capcut.com"} {
		if !domainAllowed(domain, allowed) {
			t.Fatalf("expected JiMeng cookie domain %s to be allowed", domain)
		}
	}
	for _, domain := range []string{"eviljianying.com", "capcut.com.attacker.test", "example.com"} {
		if domainAllowed(domain, allowed) {
			t.Fatalf("unexpected cookie domain %s accepted", domain)
		}
	}
}

func TestDoubaoLabKeepsItsOwnPurposeScopedProfile(t *testing.T) {
	doubao := DesiredSlot{
		BridgeID: "doubao-1", LocalPort: 9226, ServerPort: 9326,
		SSHHost: "host", SSHUser: "user", SSHPort: 22,
		Purpose: "doubao_lab", TargetURL: "https://www.doubao.com/chat/",
		CaptureID: "capture-a", LoginOnly: true,
	}
	if !sameSlot(doubao, doubao) {
		t.Fatal("identical Doubao slot must retain its fixed Chrome profile")
	}
	jimeng := doubao
	jimeng.Purpose = "jimeng_lab"
	if sameSlot(doubao, jimeng) {
		t.Fatal("Doubao and JiMeng accounts must never share a browser runtime")
	}
	changed := doubao
	changed.CaptureID = "capture-b"
	if sameSlot(doubao, changed) {
		t.Fatal("new Doubao capture cycle must restart only its exact profile")
	}
}

func TestDoubaoCookieDomainsAcceptOnlyDoubaoParents(t *testing.T) {
	allowed := []string{"doubao.com"}
	for _, domain := range []string{".doubao.com", "www.doubao.com"} {
		if !domainAllowed(domain, allowed) {
			t.Fatalf("expected Doubao cookie domain %s to be allowed", domain)
		}
	}
	for _, domain := range []string{"evildoubao.com", "doubao.com.attacker.test", "jianying.com"} {
		if domainAllowed(domain, allowed) {
			t.Fatalf("unexpected cookie domain %s accepted", domain)
		}
	}
}

func TestDoubaoDesktopSchemeIsPurposeScoped(t *testing.T) {
	if !stringAllowed("doubao", []string{"doubao"}) {
		t.Fatal("Doubao desktop scheme must be accepted by the Doubao-only probe")
	}
	for _, value := range []string{"file", "chrome", "javascript", "https"} {
		if stringAllowed(value, []string{"doubao"}) {
			t.Fatalf("unexpected internal scheme accepted: %s", value)
		}
	}
}
