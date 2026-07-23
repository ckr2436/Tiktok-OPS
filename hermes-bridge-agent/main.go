package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/gorilla/websocket"
)

const (
	configMarker = "\nMYUPONA_BRIDGE_AGENT_CONFIG_V1\n"
	agentVersion = "2026.07.18.2"
)

type Config struct {
	APIURL        string `json:"api_url"`
	Token         string `json:"token"`
	WorkspaceID   int    `json:"workspace_id"`
	UserID        int    `json:"user_id"`
	DeviceID      string `json:"device_id"`
	DeviceName    string `json:"device_name"`
	LocalCapacity int    `json:"local_capacity"`
}

type SlotStatus struct {
	BridgeID    string      `json:"bridge_id"`
	Connected   bool        `json:"connected"`
	Mode        string      `json:"mode,omitempty"`
	Browser     string      `json:"browser,omitempty"`
	Error       string      `json:"error,omitempty"`
	SyncedFiles []InboxFile `json:"synced_files,omitempty"`
	LastSyncAt  string      `json:"last_sync_at,omitempty"`
	SyncError   string      `json:"sync_error,omitempty"`
	AuthStatus  string      `json:"auth_status,omitempty"`
	AccountName string      `json:"account_name,omitempty"`
	PageURL     string      `json:"page_url,omitempty"`
}

type Heartbeat struct {
	DeviceID      string       `json:"device_id"`
	DeviceName    string       `json:"device_name"`
	AgentVersion  string       `json:"agent_version"`
	PublicKey     string       `json:"public_key"`
	InboxRoot     string       `json:"inbox_root"`
	LocalCapacity int          `json:"local_capacity"`
	Slots         []SlotStatus `json:"slots"`
}

type DesiredSlot struct {
	BridgeID         string `json:"bridge_id"`
	Desired          bool   `json:"desired"`
	LocalPort        int    `json:"local_port"`
	ServerPort       int    `json:"server_port"`
	SSHHost          string `json:"ssh_host"`
	SSHUser          string `json:"ssh_user"`
	SSHPort          int    `json:"ssh_port"`
	InboxRoot        string `json:"inbox_root"`
	ActiveProjectID  *int   `json:"active_project_id"`
	Mode             string `json:"mode"`
	RestartRequired  bool   `json:"restart_required"`
	ServerProbeError string `json:"server_probe_error"`
}

type InboxFile struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	MTime int64  `json:"mtime"`
}

type HeartbeatResponse struct {
	PollSeconds    int           `json:"poll_seconds"`
	AgentVersion   string        `json:"agent_version"`
	UpdateRequired bool          `json:"update_required"`
	Slots          []DesiredSlot `json:"slots"`
	InboxFiles     []InboxFile   `json:"inbox_files"`
}

type slotRuntime struct {
	mu            sync.Mutex
	spec          DesiredSlot
	chrome        *exec.Cmd
	ssh           *exec.Cmd
	connected     bool
	browser       string
	err           string
	stopping      bool
	authStatus    string
	accountName   string
	pageURL       string
	lastAuthCheck time.Time
	authChecking  bool
}

type cdpPage struct {
	Type                 string `json:"type"`
	URL                  string `json:"url"`
	WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
}

type cdpResponse struct {
	ID     int `json:"id"`
	Result struct {
		Result struct {
			Value any `json:"value"`
		} `json:"result"`
	} `json:"result"`
}

type chatGPTAuthProbe struct {
	Status      string `json:"status"`
	AccountName string `json:"account_name"`
	PageURL     string `json:"page_url"`
}

type slotRetryState struct {
	Failures int
	NextAt   time.Time
}

type agent struct {
	config     Config
	root       string
	inbox      string
	privateKey string
	publicKey  string
	httpClient *http.Client
	mu         sync.Mutex
	slots      map[string]*slotRuntime
	// dormant slots retain their exact profile identity but intentionally have
	// no Chrome/SSH process while an API-only stage is running.
	dormant map[string]DesiredSlot
	// A server restart request is level-triggered and can remain true while
	// Chrome is still starting. Remember that it was consumed so heartbeats do
	// not repeatedly kill the same recovery attempt.
	serverRestartSeen map[string]bool
	retryMu           sync.Mutex
	retries           map[string]slotRetryState
	syncMu            sync.Mutex
	syncedFiles       []InboxFile
	lastSyncAt        string
	syncError         string
}

func main() {
	if runtime.GOOS != "windows" {
		return
	}
	mode := ""
	if len(os.Args) >= 2 {
		mode = os.Args[1]
	}
	if mode == "--update-install" {
		if err := install(); err != nil {
			writeFallbackLog(err)
		}
		return
	}
	if mode != "--run" {
		if err := install(); err != nil {
			messageBox("MYUPONA Hermes Bridge", "Installation failed:\n"+err.Error())
		} else {
			messageBox("MYUPONA Hermes Bridge", "Installed successfully. The browser agent is now running and will start automatically at sign-in.")
		}
		return
	}
	if !acquireMutex("Local\\MYUPONA_HermesBridgeAgent") {
		return
	}
	removeLegacyTasks()
	cleanupLegacyBridgeProcesses()
	a, err := newAgent()
	if err != nil {
		writeFallbackLog(err)
		return
	}
	if err := cleanupLegacyInbox(os.Getenv("LOCALAPPDATA")); err != nil {
		a.logf("legacy inbox cleanup warning: %v", err)
	}
	a.logf("agent started version=%s device=%s", agentVersion, a.config.DeviceID)
	a.run()
}

func install() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	data, err := os.ReadFile(exe)
	if err != nil {
		return err
	}
	config, err := embeddedConfig(data)
	if err != nil {
		return err
	}
	local := os.Getenv("LOCALAPPDATA")
	if local == "" {
		return errors.New("LOCALAPPDATA is not available")
	}
	root := filepath.Join(local, "MYUPONA", "HermesBridgeAgent")
	if err := os.MkdirAll(root, 0700); err != nil {
		return err
	}
	target := filepath.Join(root, "MYUPONA-HermesBridge.exe")
	if !samePath(exe, target) {
		if err := os.WriteFile(target, data, 0700); err != nil {
			return err
		}
	}
	configData, _ := json.MarshalIndent(config, "", "  ")
	if err := os.WriteFile(filepath.Join(root, "agent.json"), configData, 0600); err != nil {
		return err
	}
	removeLegacyTasks()
	cleanupLegacyBridgeProcesses()
	runValue := fmt.Sprintf("\"%s\" --run", target)
	cmd := hiddenCommand("reg.exe", "add", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, "/v", "MYUPONA Hermes Bridge Agent", "/t", "REG_SZ", "/d", runValue, "/f")
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("register startup: %w: %s", err, strings.TrimSpace(string(output)))
	}
	killExistingAgents()
	return hiddenCommand(target, "--run").Start()
}

func embeddedConfig(data []byte) (Config, error) {
	index := bytes.LastIndex(data, []byte(configMarker))
	if index < 0 {
		return Config{}, errors.New("this executable is not personalized; download it from GMV Ops again")
	}
	var config Config
	if err := json.Unmarshal(data[index+len(configMarker):], &config); err != nil {
		return Config{}, fmt.Errorf("read embedded configuration: %w", err)
	}
	if config.APIURL == "" || config.Token == "" || config.DeviceID == "" {
		return Config{}, errors.New("embedded configuration is incomplete")
	}
	return config, nil
}

func newAgent() (*agent, error) {
	local := os.Getenv("LOCALAPPDATA")
	root := filepath.Join(local, "MYUPONA", "HermesBridgeAgent")
	data, err := os.ReadFile(filepath.Join(root, "agent.json"))
	if err != nil {
		return nil, err
	}
	var config Config
	if err := json.Unmarshal(data, &config); err != nil {
		return nil, err
	}
	if config.LocalCapacity < 1 {
		config.LocalCapacity = 4
	}
	inbox := accountInboxRoot(local, config)
	if err := os.MkdirAll(inbox, 0700); err != nil {
		return nil, err
	}
	a := &agent{
		config:            config,
		root:              root,
		inbox:             inbox,
		privateKey:        filepath.Join(root, "agent_ed25519"),
		httpClient:        &http.Client{Timeout: 45 * time.Second},
		slots:             make(map[string]*slotRuntime),
		dormant:           make(map[string]DesiredSlot),
		serverRestartSeen: make(map[string]bool),
		retries:           make(map[string]slotRetryState),
	}
	if err := a.ensureKey(); err != nil {
		return nil, err
	}
	return a, nil
}

func accountInboxRoot(local string, config Config) string {
	root := filepath.Join(local, "MYUPONA", "HermesInbox")
	if config.WorkspaceID > 0 && config.UserID > 0 {
		return filepath.Join(
			root,
			fmt.Sprintf("workspace_%d", config.WorkspaceID),
			fmt.Sprintf("user_%d", config.UserID),
		)
	}
	return root
}

func cleanupLegacyInbox(local string) error {
	root := filepath.Join(local, "MYUPONA", "HermesInbox")
	entries, err := os.ReadDir(root)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		path := filepath.Join(root, entry.Name())
		if entry.IsDir() && strings.HasPrefix(strings.ToLower(entry.Name()), "workspace_") {
			children, readErr := os.ReadDir(path)
			if readErr != nil {
				return readErr
			}
			for _, child := range children {
				// New account-scoped caches live below user_N. Everything else
				// in a workspace folder belongs to the retired shared-inbox format.
				if child.IsDir() && strings.HasPrefix(strings.ToLower(child.Name()), "user_") {
					continue
				}
				if err := os.RemoveAll(filepath.Join(path, child.Name())); err != nil {
					return err
				}
			}
			continue
		}
		// The legacy dynamic agent wrote project_key folders directly under
		// HermesInbox. Preserve unknown regular files, but remove those folders.
		if entry.IsDir() && strings.HasPrefix(strings.ToLower(entry.Name()), "cf_") {
			if err := os.RemoveAll(path); err != nil {
				return err
			}
		}
	}
	return nil
}

func (a *agent) ensureKey() error {
	if _, err := os.Stat(a.privateKey); os.IsNotExist(err) {
		cmd := hiddenCommand("ssh-keygen.exe", "-q", "-t", "ed25519", "-N", "", "-C", "MYUPONA-HermesBridge", "-f", a.privateKey)
		if output, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("generate SSH key: %w: %s", err, strings.TrimSpace(string(output)))
		}
	}
	public, err := os.ReadFile(a.privateKey + ".pub")
	if err != nil {
		return err
	}
	a.publicKey = strings.TrimSpace(string(public))
	return nil
}

func (a *agent) run() {
	delay := time.Second
	updateRetryAt := time.Time{}
	for {
		response, err := a.heartbeat()
		if err != nil {
			a.logf("heartbeat failed: %v", err)
			time.Sleep(delay)
			if delay < 30*time.Second {
				delay *= 2
			}
			continue
		}
		delay = time.Second
		if requiresAgentUpdate(agentVersion, response.AgentVersion, response.UpdateRequired) && time.Now().After(updateRetryAt) {
			targetVersion := strings.TrimSpace(response.AgentVersion)
			if err := a.installUpdate(targetVersion); err != nil {
				a.logf("agent update failed target=%s: %v", targetVersion, err)
				updateRetryAt = time.Now().Add(5 * time.Minute)
			} else {
				a.logf("agent update scheduled current=%s target=%s", agentVersion, targetVersion)
				a.stopAll()
				return
			}
		}
		a.reconcile(response.Slots)
		if len(response.InboxFiles) > 0 {
			a.logf("sync inbox request files=%d", len(response.InboxFiles))
		}
		if err := a.syncInbox(response.InboxFiles); err != nil {
			a.logf("inbox sync warning: %v", err)
			a.syncMu.Lock()
			a.syncError = err.Error()
			a.syncMu.Unlock()
		}
		poll := response.PollSeconds
		if poll < 2 || poll > 30 {
			poll = 3
		}
		time.Sleep(time.Duration(poll) * time.Second)
	}
}

func (a *agent) heartbeat() (HeartbeatResponse, error) {
	body := Heartbeat{
		DeviceID:      a.config.DeviceID,
		DeviceName:    a.config.DeviceName,
		AgentVersion:  agentVersion,
		PublicKey:     a.publicKey,
		InboxRoot:     a.inbox,
		LocalCapacity: a.config.LocalCapacity,
		Slots:         a.statuses(),
	}
	encoded, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, a.config.APIURL+"/heartbeat", bytes.NewReader(encoded))
	if err != nil {
		return HeartbeatResponse{}, err
	}
	req.Header.Set("Authorization", "Bearer "+a.config.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return HeartbeatResponse{}, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if resp.StatusCode != http.StatusOK {
		return HeartbeatResponse{}, fmt.Errorf("server returned %s: %s", resp.Status, strings.TrimSpace(string(data)))
	}
	var result HeartbeatResponse
	if err := json.Unmarshal(data, &result); err != nil {
		return result, err
	}
	return result, nil
}

func (a *agent) statuses() []SlotStatus {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.syncMu.Lock()
	syncedFiles := append([]InboxFile(nil), a.syncedFiles...)
	lastSyncAt := a.lastSyncAt
	syncError := a.syncError
	a.syncMu.Unlock()
	result := make([]SlotStatus, 0, len(a.slots)+len(a.dormant))
	for _, slot := range a.slots {
		slot.mu.Lock()
		result = append(result, SlotStatus{
			BridgeID: slot.spec.BridgeID, Connected: slot.connected, Mode: "active", Browser: slot.browser, Error: slot.err,
			SyncedFiles: syncedFiles, LastSyncAt: lastSyncAt, SyncError: syncError,
			AuthStatus: slot.authStatus, AccountName: slot.accountName, PageURL: slot.pageURL,
		})
		slot.mu.Unlock()
	}
	for _, spec := range a.dormant {
		result = append(result, SlotStatus{
			BridgeID: spec.BridgeID, Connected: false, Mode: "dormant",
			SyncedFiles: syncedFiles, LastSyncAt: lastSyncAt, SyncError: syncError,
			AuthStatus: "dormant",
		})
	}
	return result
}

func (a *agent) reconcile(desired []DesiredSlot) {
	wanted := make(map[string]DesiredSlot, len(desired))
	for _, spec := range desired {
		if spec.Desired {
			wanted[spec.BridgeID] = spec
		}
	}
	a.mu.Lock()
	for id, spec := range wanted {
		if slotMode(spec.Mode) == "dormant" {
			if running, ok := a.slots[id]; ok {
				running.stop()
				delete(a.slots, id)
			}
			a.dormant[id] = spec
			delete(a.serverRestartSeen, id)
			continue
		}
		if !spec.RestartRequired {
			delete(a.serverRestartSeen, id)
		}
	}
	for id, running := range a.slots {
		spec, ok := wanted[id]
		restartRequested := ok && slotMode(spec.Mode) == "active" && spec.RestartRequired && !a.serverRestartSeen[id]
		if !ok || slotMode(spec.Mode) != "active" || !sameSlot(running.spec, spec) || restartRequested {
			if restartRequested {
				running.setError(fmt.Errorf("server requested slot restart: %s", strings.TrimSpace(spec.ServerProbeError)))
				a.logf("slot restart requested bridge=%s reason=%s", id, strings.TrimSpace(spec.ServerProbeError))
				a.serverRestartSeen[id] = true
			}
			running.stop()
			delete(a.slots, id)
			if !ok {
				delete(a.serverRestartSeen, id)
			}
		}
	}
	for id := range a.dormant {
		spec, ok := wanted[id]
		if !ok || slotMode(spec.Mode) != "dormant" {
			delete(a.dormant, id)
			continue
		}
		a.dormant[id] = spec
	}
	for id, spec := range wanted {
		if slotMode(spec.Mode) != "active" {
			continue
		}
		delete(a.dormant, id)
		if _, ok := a.slots[id]; ok {
			continue
		}
		if !a.slotRetryReady(id) {
			continue
		}
		running := &slotRuntime{spec: spec}
		a.slots[id] = running
		go a.startSlot(running)
	}
	a.mu.Unlock()
}

func (a *agent) startSlot(slot *slotRuntime) {
	spec := slot.spec
	startedAt := time.Now()
	defer func() {
		slot.mu.Lock()
		stopping := slot.stopping
		lastError := strings.TrimSpace(slot.err)
		slot.mu.Unlock()
		if !stopping {
			a.recordSlotFailure(spec.BridgeID, startedAt, lastError)
		}
		a.mu.Lock()
		if current, ok := a.slots[spec.BridgeID]; ok && current == slot {
			delete(a.slots, spec.BridgeID)
		}
		a.mu.Unlock()
	}()
	profilesRoot := filepath.Join(a.root, "profiles")
	profile := filepath.Join(profilesRoot, fmt.Sprintf("slot-%d", spec.LocalPort))
	legacyProfile := filepath.Join(profilesRoot, spec.BridgeID)
	if _, err := os.Stat(profile); os.IsNotExist(err) {
		if _, legacyErr := os.Stat(legacyProfile); legacyErr == nil {
			_ = os.Rename(legacyProfile, profile)
		}
	}
	_ = os.MkdirAll(profile, 0700)
	cdpPort := 0
	if waitCDP(spec.LocalPort, 500*time.Millisecond) {
		// Compatibility with profiles created by older Agent versions.
		cdpPort = spec.LocalPort
	} else {
		cdpPort = waitProfileCDP(profile, 500*time.Millisecond)
	}
	if cdpPort == 0 {
		// Chrome 150 can ignore a fixed remote-debugging port while still
		// opening a normal browser window.  Use Chrome's secure ephemeral port
		// and discover it through DevToolsActivePort.  First remove every
		// process tied to this exact profile so an orphaned crashpad/renderer
		// cannot keep the profile lock and turn the next launch into a no-op.
		killChromeForDebugPort(spec.LocalPort)
		_ = os.Remove(filepath.Join(profile, "DevToolsActivePort"))
		_ = os.Remove(filepath.Join(profile, "lockfile"))
		chrome, err := chromePath()
		if err != nil {
			slot.setError(err)
			return
		}
		cmd := hiddenCommand(chrome,
			"--remote-debugging-address=127.0.0.1",
			"--remote-debugging-port=0",
			"--remote-allow-origins=*",
			"--user-data-dir="+profile,
			"--no-first-run", "--no-default-browser-check", "--new-window", "https://chatgpt.com/")
		if err := cmd.Start(); err != nil {
			slot.setError(fmt.Errorf("start Chrome: %w", err))
			return
		}
		slot.mu.Lock()
		slot.chrome = cmd
		slot.mu.Unlock()
		go cmd.Wait()
		cdpPort = waitProfileCDP(profile, 45*time.Second)
		if cdpPort == 0 {
			slot.setError(errors.New("Chrome CDP did not become ready"))
			return
		}
	}
	knownHosts := filepath.Join(a.root, "known_hosts")
	target := spec.SSHUser + "@" + spec.SSHHost
	args := []string{
		"-N", "-T", "-i", a.privateKey, "-p", strconv.Itoa(spec.SSHPort),
		"-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + knownHosts,
		"-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
		"-R", fmt.Sprintf("127.0.0.1:%d:127.0.0.1:%d", spec.ServerPort, cdpPort), target,
	}
	cmd := hiddenCommand("ssh.exe", args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		slot.setError(fmt.Errorf("start SSH tunnel: %w", err))
		return
	}
	slot.mu.Lock()
	slot.ssh = cmd
	slot.connected = true
	slot.browser = browserVersion(cdpPort)
	slot.err = ""
	slot.authStatus = "checking"
	slot.mu.Unlock()
	go refreshSlotAuth(slot, cdpPort)
	a.logf("slot connected bridge=%s profile_slot=%d cdp=%d remote=%d", spec.BridgeID, spec.LocalPort, cdpPort, spec.ServerPort)
	sshDone := make(chan error, 1)
	go func() { sshDone <- cmd.Wait() }()
	watchdog := time.NewTicker(5 * time.Second)
	defer watchdog.Stop()
	cdpFailures := 0
	stableReset := false
	for {
		select {
		case err := <-sshDone:
			slot.mu.Lock()
			slot.connected = false
			if !slot.stopping {
				slot.err = strings.TrimSpace(stderr.String())
				if slot.err == "" {
					if err != nil {
						slot.err = err.Error()
					} else {
						slot.err = "SSH tunnel exited unexpectedly"
					}
				}
			}
			slot.mu.Unlock()
			return
		case <-watchdog.C:
			probe, probeErr := probeChatGPTAuth(cdpPort)
			if waitCDP(cdpPort, 2*time.Second) && probeErr == nil {
				cdpFailures = 0
				slot.mu.Lock()
				slot.authStatus = probe.Status
				slot.accountName = probe.AccountName
				slot.pageURL = probe.PageURL
				slot.err = ""
				slot.mu.Unlock()
				if !stableReset && time.Since(startedAt) >= 2*time.Minute {
					a.clearSlotRetry(spec.BridgeID)
					stableReset = true
				}
				continue
			}
			cdpFailures++
			if cdpFailures < 3 {
				continue
			}
			message := fmt.Sprintf("local Chrome page CDP stopped responding on port %d", cdpPort)
			if probeErr != nil {
				message += ": " + probeErr.Error()
			}
			slot.mu.Lock()
			slot.connected = false
			slot.err = message
			if slot.ssh != nil && slot.ssh.Process != nil {
				_ = slot.ssh.Process.Kill()
			}
			if slot.chrome != nil && slot.chrome.Process != nil {
				_ = slot.chrome.Process.Kill()
			}
			slot.mu.Unlock()
			killChromeForDebugPort(spec.LocalPort)
			a.logf("slot watchdog restarting bridge=%s reason=%s", spec.BridgeID, message)
			return
		}
	}
}

func (s *slotRuntime) setError(err error) {
	s.mu.Lock()
	s.connected = false
	s.err = err.Error()
	s.mu.Unlock()
}

func (s *slotRuntime) stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stopping = true
	if s.ssh != nil && s.ssh.Process != nil {
		_ = s.ssh.Process.Kill()
	}
	if s.chrome != nil && s.chrome.Process != nil {
		_ = s.chrome.Process.Kill()
	}
	killChromeForDebugPort(s.spec.LocalPort)
	s.connected = false
}

func killChromeForDebugPort(port int) {
	if port <= 0 {
		return
	}
	// Chrome may outlive the process handle that originally launched it (for
	// example after an agent auto-update). Match only this slot's debugging
	// port so recovering one project cannot close another project's browser.
	pattern := fmt.Sprintf(`--remote-debugging-port(?:=|\s+)%d(?:\s|$)`, port)
	profilePattern := fmt.Sprintf(`HermesBridgeAgent[\\/]+profiles[\\/]+slot-%d(?:[\\/\"\s]|$)`, port)
	script := fmt.Sprintf(`
$pattern = '%s'
$profilePattern = '%s'
Get-CimInstance Win32_Process |
  Where-Object {
    ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and
    ($_.CommandLine -match $pattern -or $_.CommandLine -match $profilePattern)
  } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
  }
`, strings.ReplaceAll(pattern, "'", "''"), strings.ReplaceAll(profilePattern, "'", "''"))
	_ = hiddenCommand(
		"powershell.exe",
		"-NoProfile",
		"-ExecutionPolicy", "Bypass",
		"-WindowStyle", "Hidden",
		"-Command", script,
	).Run()
}

func (a *agent) slotRetryReady(bridgeID string) bool {
	a.retryMu.Lock()
	defer a.retryMu.Unlock()
	state, ok := a.retries[bridgeID]
	return !ok || !time.Now().Before(state.NextAt)
}

func (a *agent) clearSlotRetry(bridgeID string) {
	a.retryMu.Lock()
	delete(a.retries, bridgeID)
	a.retryMu.Unlock()
}

func (a *agent) recordSlotFailure(bridgeID string, startedAt time.Time, message string) {
	a.retryMu.Lock()
	state := a.retries[bridgeID]
	if time.Since(startedAt) >= 2*time.Minute {
		state.Failures = 0
	}
	state.Failures++
	delays := []time.Duration{5 * time.Second, 15 * time.Second, time.Minute, 5 * time.Minute, 15 * time.Minute}
	index := state.Failures - 1
	if index >= len(delays) {
		index = len(delays) - 1
	}
	state.NextAt = time.Now().Add(delays[index])
	a.retries[bridgeID] = state
	a.retryMu.Unlock()
	a.logf(
		"slot restart backoff bridge=%s failures=%d delay=%s error=%s",
		bridgeID,
		state.Failures,
		delays[index],
		strings.TrimSpace(message),
	)
}

func (a *agent) stopAll() {
	a.mu.Lock()
	for id, slot := range a.slots {
		slot.stop()
		delete(a.slots, id)
	}
	for id := range a.dormant {
		delete(a.dormant, id)
	}
	a.mu.Unlock()
}

func (a *agent) installUpdate(targetVersion string) error {
	req, err := http.NewRequest(http.MethodGet, a.config.APIURL+"/update", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.config.Token)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("download agent %s: %s: %s", targetVersion, resp.Status, strings.TrimSpace(string(data)))
	}
	target, err := os.Executable()
	if err != nil {
		return err
	}
	next := filepath.Join(a.root, "MYUPONA-HermesBridge.next.exe")
	out, err := os.Create(next)
	if err != nil {
		return err
	}
	written, copyErr := io.Copy(out, io.LimitReader(resp.Body, 64<<20))
	closeErr := out.Close()
	if copyErr != nil {
		return copyErr
	}
	if closeErr != nil {
		return closeErr
	}
	if written < 1<<20 {
		return fmt.Errorf("downloaded agent is unexpectedly small: %d bytes", written)
	}
	header := make([]byte, 2)
	file, err := os.Open(next)
	if err != nil {
		return err
	}
	_, readErr := io.ReadFull(file, header)
	_ = file.Close()
	if readErr != nil || string(header) != "MZ" {
		return errors.New("downloaded agent is not a Windows executable")
	}
	script := fmt.Sprintf(`
$pidToWait = %d
$source = %s
$target = %s
Wait-Process -Id $pidToWait -ErrorAction SilentlyContinue
$installed = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    Move-Item -LiteralPath $source -Destination $target -Force -ErrorAction Stop
    $installed = $true
    break
  } catch {
    Start-Sleep -Seconds 1
  }
}
if ($installed) {
  Start-Process -FilePath $target -ArgumentList '--update-install' -WindowStyle Hidden
}
`, os.Getpid(), psSingleQuote(next), psSingleQuote(target))
	return hiddenCommand(
		"powershell.exe",
		"-NoProfile",
		"-ExecutionPolicy", "Bypass",
		"-WindowStyle", "Hidden",
		"-Command", script,
	).Start()
}

func psSingleQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}

func (a *agent) syncInbox(files []InboxFile) error {
	for _, item := range files {
		relative := filepath.Clean(filepath.FromSlash(item.Path))
		if relative == "." || filepath.IsAbs(relative) || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
			continue
		}
		target := filepath.Join(a.inbox, relative)
		if stat, err := os.Stat(target); err == nil && stat.Size() == item.Size && stat.ModTime().Unix() >= item.MTime {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return err
		}
		fileURL := a.config.APIURL + "/inbox/" + strings.ReplaceAll(url.PathEscape(item.Path), "%2F", "/")
		req, _ := http.NewRequest(http.MethodGet, fileURL, nil)
		req.Header.Set("Authorization", "Bearer "+a.config.Token)
		resp, err := a.httpClient.Do(req)
		if err != nil {
			return err
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return fmt.Errorf("download %s: %s", item.Path, resp.Status)
		}
		temp := target + ".part"
		out, err := os.Create(temp)
		if err != nil {
			resp.Body.Close()
			return err
		}
		_, copyErr := io.Copy(out, resp.Body)
		out.Close()
		resp.Body.Close()
		if copyErr != nil {
			return copyErr
		}
		_ = os.Chtimes(temp, time.Unix(item.MTime, 0), time.Unix(item.MTime, 0))
		if err := os.Rename(temp, target); err != nil {
			return err
		}
	}
	if err := a.pruneInbox(files); err != nil {
		return err
	}
	confirmed := make([]InboxFile, 0, len(files))
	for _, item := range files {
		relative := filepath.Clean(filepath.FromSlash(item.Path))
		if relative == "." || filepath.IsAbs(relative) || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
			continue
		}
		target := filepath.Join(a.inbox, relative)
		stat, err := os.Stat(target)
		if err != nil || stat.Size() != item.Size {
			continue
		}
		confirmed = append(confirmed, InboxFile{
			Path: item.Path, Size: stat.Size(), MTime: stat.ModTime().Unix(),
		})
	}
	a.syncMu.Lock()
	a.syncedFiles = confirmed
	a.lastSyncAt = time.Now().UTC().Format(time.RFC3339)
	a.syncError = ""
	a.syncMu.Unlock()
	return nil
}

func (a *agent) pruneInbox(files []InboxFile) error {
	desired := make(map[string]bool, len(files))
	for _, item := range files {
		relative := filepath.Clean(filepath.FromSlash(item.Path))
		if relative == "." || filepath.IsAbs(relative) || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
			continue
		}
		desired[strings.ToLower(filepath.Clean(filepath.Join(a.inbox, relative)))] = true
	}
	directories := make([]string, 0)
	err := filepath.WalkDir(a.inbox, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if samePath(path, a.inbox) {
			return nil
		}
		if entry.IsDir() {
			directories = append(directories, path)
			return nil
		}
		if !desired[strings.ToLower(filepath.Clean(path))] {
			if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
				return err
			}
		}
		return nil
	})
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	for index := len(directories) - 1; index >= 0; index-- {
		_ = os.Remove(directories[index])
	}
	return nil
}

func waitCDP(port int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		client := http.Client{Timeout: time.Second}
		resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/json/version", port))
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return true
			}
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func waitProfileCDP(profile string, timeout time.Duration) int {
	deadline := time.Now().Add(timeout)
	for {
		data, err := os.ReadFile(filepath.Join(profile, "DevToolsActivePort"))
		if err == nil {
			lines := strings.Split(strings.TrimSpace(string(data)), "\n")
			if len(lines) > 0 {
				port, parseErr := strconv.Atoi(strings.TrimSpace(lines[0]))
				if parseErr == nil && port > 0 && port <= 65535 && waitCDP(port, 500*time.Millisecond) {
					return port
				}
			}
		}
		if time.Now().After(deadline) {
			return 0
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func browserVersion(port int) string {
	resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/json/version", port))
	if err != nil {
		return "Chrome"
	}
	defer resp.Body.Close()
	var data map[string]any
	if json.NewDecoder(resp.Body).Decode(&data) == nil {
		if value, ok := data["Browser"].(string); ok {
			return value
		}
	}
	return "Chrome"
}

func refreshSlotAuth(slot *slotRuntime, port int) {
	slot.mu.Lock()
	if slot.authChecking || (!slot.lastAuthCheck.IsZero() && time.Since(slot.lastAuthCheck) < 12*time.Second) {
		slot.mu.Unlock()
		return
	}
	slot.authChecking = true
	slot.lastAuthCheck = time.Now()
	slot.mu.Unlock()

	probe, err := probeChatGPTAuth(port)
	slot.mu.Lock()
	defer slot.mu.Unlock()
	slot.authChecking = false
	if err != nil {
		if slot.authStatus == "" {
			slot.authStatus = "checking"
		}
		return
	}
	slot.authStatus = probe.Status
	slot.accountName = probe.AccountName
	slot.pageURL = probe.PageURL
}

func probeChatGPTAuth(port int) (chatGPTAuthProbe, error) {
	probe := chatGPTAuthProbe{Status: "checking"}
	client := http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/json/list", port))
	if err != nil {
		return probe, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return probe, fmt.Errorf("CDP page list returned %s", resp.Status)
	}
	var pages []cdpPage
	if err := json.NewDecoder(resp.Body).Decode(&pages); err != nil {
		return probe, err
	}
	var page cdpPage
	for _, candidate := range pages {
		if candidate.Type == "page" && strings.Contains(strings.ToLower(candidate.URL), "chatgpt.com") {
			page = candidate
			break
		}
	}
	if page.WebSocketDebuggerURL == "" {
		return probe, nil
	}
	probe.PageURL = page.URL
	dialer := websocket.Dialer{HandshakeTimeout: 5 * time.Second}
	conn, _, err := dialer.Dial(page.WebSocketDebuggerURL, http.Header{"Origin": []string{"http://127.0.0.1"}})
	if err != nil {
		return probe, err
	}
	defer conn.Close()
	deadline := time.Now().Add(6 * time.Second)
	_ = conn.SetReadDeadline(deadline)
	_ = conn.SetWriteDeadline(deadline)
	expression := `JSON.stringify((() => {
  const bootstrap = [...document.scripts].map((node) => node.textContent || '').join('\n');
  const body = document.body?.innerText || '';
  const accountButton = document.querySelector('[data-testid="accounts-profile-button"], button[aria-label*="Profile"], button[aria-label*="profile"], button[aria-label*="账号"], button[aria-label*="账户"], button[aria-label*="个人资料"]');
  const composer = Boolean(document.querySelector('#prompt-textarea, textarea[data-id="root"]'));
  const loggedIn = bootstrap.includes('logged_in') || Boolean(accountButton);
  const loggedOut = bootstrap.includes('logged_out') || /Log in|Sign up|登录|注册/.test(body.slice(0, 2000));
  let status = 'checking';
  if (loggedIn && composer) status = 'ready';
  else if (loggedOut && !loggedIn) status = 'login_required';
  const accountName = (accountButton?.innerText || accountButton?.getAttribute('aria-label') || '').trim().slice(0, 120);
  return {status, account_name: accountName, page_url: location.href};
})())`
	request := map[string]any{
		"id":     1,
		"method": "Runtime.evaluate",
		"params": map[string]any{"expression": expression, "returnByValue": true, "awaitPromise": true},
	}
	if err := conn.WriteJSON(request); err != nil {
		return probe, err
	}
	for {
		var message cdpResponse
		if err := conn.ReadJSON(&message); err != nil {
			return probe, err
		}
		if message.ID != 1 {
			continue
		}
		value, ok := message.Result.Result.Value.(string)
		if !ok || strings.TrimSpace(value) == "" {
			return probe, errors.New("CDP auth probe returned no value")
		}
		if err := json.Unmarshal([]byte(value), &probe); err != nil {
			return probe, err
		}
		if probe.Status == "" {
			probe.Status = "checking"
		}
		return probe, nil
	}
}

func chromePath() (string, error) {
	candidates := []string{
		filepath.Join(os.Getenv("PROGRAMFILES"), "Google", "Chrome", "Application", "chrome.exe"),
		filepath.Join(os.Getenv("PROGRAMFILES(X86)"), "Google", "Chrome", "Application", "chrome.exe"),
		filepath.Join(os.Getenv("LOCALAPPDATA"), "Google", "Chrome", "Application", "chrome.exe"),
		filepath.Join(os.Getenv("PROGRAMFILES"), "Microsoft", "Edge", "Application", "msedge.exe"),
	}
	for _, candidate := range candidates {
		if candidate != "" {
			if _, err := os.Stat(candidate); err == nil {
				return candidate, nil
			}
		}
	}
	return "", errors.New("Chrome or Edge was not found")
}

func hiddenCommand(name string, args ...string) *exec.Cmd {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	return cmd
}

func sameSlot(left, right DesiredSlot) bool {
	return left.BridgeID == right.BridgeID && left.LocalPort == right.LocalPort && left.ServerPort == right.ServerPort &&
		left.SSHHost == right.SSHHost && left.SSHUser == right.SSHUser && left.SSHPort == right.SSHPort
}

func requiresAgentUpdate(currentVersion, targetVersion string, serverRequested bool) bool {
	return serverRequested && strings.TrimSpace(targetVersion) != "" &&
		strings.TrimSpace(targetVersion) != strings.TrimSpace(currentVersion)
}

func slotMode(value string) string {
	if strings.EqualFold(strings.TrimSpace(value), "dormant") {
		return "dormant"
	}
	return "active"
}

func samePath(a, b string) bool {
	left, _ := filepath.Abs(a)
	right, _ := filepath.Abs(b)
	return strings.EqualFold(left, right)
}

func removeLegacyTasks() {
	// The first bridge installer used an HKCU Run value and one long-running
	// PowerShell process that managed three fixed browser profiles. Remove that
	// entry during every agent migration so it cannot recreate legacy slots.
	_ = hiddenCommand(
		"reg.exe", "delete", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
		"/v", "MYUPONA Hermes Browser Bridge", "/f",
	).Run()
	output, err := hiddenCommand("schtasks.exe", "/Query", "/FO", "CSV", "/NH").Output()
	if err != nil {
		return
	}
	reader := csv.NewReader(bytes.NewReader(output))
	for {
		record, err := reader.Read()
		if err != nil {
			break
		}
		if len(record) > 0 && strings.Contains(record[0], "MYUPONA Hermes Browser Bridge") {
			_ = hiddenCommand("schtasks.exe", "/Delete", "/TN", record[0], "/F").Run()
		}
	}
}

func cleanupLegacyBridgeProcesses() {
	// Earlier bridge installers used separate HermesChrome/SlotN profiles.
	// Those processes are not owned by the dynamic agent and can survive task
	// removal or an upgrade, leaving several misleading Chrome windows open.
	// Match only the legacy profile roots and explicitly exclude current agent
	// profiles so an active project is never interrupted by this cleanup.
	script := `
$legacyRoot = [IO.Path]::Combine($env:LOCALAPPDATA, 'MYUPONA')
$legacyPattern = [regex]::Escape($legacyRoot + '\HermesChrome')
Get-CimInstance Win32_Process |
  Where-Object {
    (
      ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and
      $_.CommandLine -match $legacyPattern -and
      $_.CommandLine -notmatch ([regex]::Escape('\HermesBridgeAgent\profiles\'))
    ) -or (
      $_.Name -eq 'powershell.exe' -and
      $_.CommandLine -match 'Start-HermesBrowserBridge\.ps1'
    )
  } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
  }
`
	_ = hiddenCommand(
		"powershell.exe",
		"-NoProfile",
		"-ExecutionPolicy", "Bypass",
		"-WindowStyle", "Hidden",
		"-Command", script,
	).Run()
}

func killExistingAgents() {
	script := `
$current = $PID
Get-CimInstance Win32_Process |
  Where-Object {
    $_.ProcessId -ne $current -and
    $_.Name -eq 'MYUPONA-HermesBridge.exe' -and
    $_.CommandLine -like '*--run*'
  } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
  }
`
	_ = hiddenCommand(
		"powershell.exe",
		"-NoProfile",
		"-ExecutionPolicy", "Bypass",
		"-Command", script,
	).Run()
}

func acquireMutex(name string) bool {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	createMutex := kernel32.NewProc("CreateMutexW")
	namePtr, _ := syscall.UTF16PtrFromString(name)
	handle, _, callErr := createMutex.Call(0, 1, uintptr(unsafe.Pointer(namePtr)))
	if handle == 0 {
		return false
	}
	return callErr != syscall.Errno(183)
}

func messageBox(title, body string) {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	titlePtr, _ := syscall.UTF16PtrFromString(title)
	bodyPtr, _ := syscall.UTF16PtrFromString(body)
	proc.Call(0, uintptr(unsafe.Pointer(bodyPtr)), uintptr(unsafe.Pointer(titlePtr)), 0x40)
}

func (a *agent) logf(format string, args ...any) {
	line := time.Now().Format(time.RFC3339) + " " + fmt.Sprintf(format, args...) + "\r\n"
	file, err := os.OpenFile(filepath.Join(a.root, "agent.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err == nil {
		_, _ = file.WriteString(line)
		_ = file.Close()
	}
}

func writeFallbackLog(err error) {
	root := filepath.Join(os.Getenv("LOCALAPPDATA"), "MYUPONA", "HermesBridgeAgent")
	_ = os.MkdirAll(root, 0700)
	_ = os.WriteFile(filepath.Join(root, "startup-error.log"), []byte(err.Error()), 0600)
}

// Keep context imported in builds where exec cancellation is enabled later.
var _ = context.Background
