package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/gorilla/websocket"
)

const (
	configMarker = "\nMYUPONA_BRIDGE_AGENT_CONFIG_V1\n"
	agentVersion = "2026.08.11.4"
)

type Config struct {
	APIURL          string `json:"api_url"`
	Token           string `json:"token"`
	WorkspaceID     int    `json:"workspace_id"`
	UserID          int    `json:"user_id"`
	DeviceID        string `json:"device_id"`
	DeviceName      string `json:"device_name"`
	LocalCapacity   int    `json:"local_capacity"`
	ProfileCapacity int    `json:"profile_capacity"`
	RuntimePortBase int    `json:"runtime_port_base,omitempty"`
}

type SlotStatus struct {
	BridgeID           string         `json:"bridge_id"`
	Connected          bool           `json:"connected"`
	Mode               string         `json:"mode,omitempty"`
	Browser            string         `json:"browser,omitempty"`
	Error              string         `json:"error,omitempty"`
	SyncedFiles        []InboxFile    `json:"synced_files,omitempty"`
	LastSyncAt         string         `json:"last_sync_at,omitempty"`
	SyncError          string         `json:"sync_error,omitempty"`
	AuthStatus         string         `json:"auth_status,omitempty"`
	AccountName        string         `json:"account_name,omitempty"`
	PageURL            string         `json:"page_url,omitempty"`
	Purpose            string         `json:"purpose,omitempty"`
	FlowStatus         string         `json:"flow_status,omitempty"`
	CaptureID          string         `json:"capture_id,omitempty"`
	SessionDiagnostics map[string]any `json:"session_diagnostics,omitempty"`
	ProfileReset       bool           `json:"profile_reset,omitempty"`
}

type Heartbeat struct {
	DeviceID          string       `json:"device_id"`
	DeviceName        string       `json:"device_name"`
	AgentVersion      string       `json:"agent_version"`
	PublicKey         string       `json:"public_key"`
	InboxRoot         string       `json:"inbox_root"`
	LocalCapacity     int          `json:"local_capacity"`
	ProfileCapacity   int          `json:"profile_capacity"`
	HostID            string       `json:"host_id"`
	InstalledBindings []string     `json:"installed_bindings,omitempty"`
	UpdateState       string       `json:"update_state,omitempty"`
	UpdateError       string       `json:"update_error,omitempty"`
	Slots             []SlotStatus `json:"slots"`
}

type installedBinding struct {
	config Config
	root   string
}

type updateStatus struct {
	State string `json:"state,omitempty"`
	Error string `json:"error,omitempty"`
}

type DesiredSlot struct {
	BridgeID         string   `json:"bridge_id"`
	Desired          bool     `json:"desired"`
	LocalPort        int      `json:"local_port"`
	ServerPort       int      `json:"server_port"`
	SSHHost          string   `json:"ssh_host"`
	SSHUser          string   `json:"ssh_user"`
	SSHPort          int      `json:"ssh_port"`
	InboxRoot        string   `json:"inbox_root"`
	ActiveProjectID  *int     `json:"active_project_id"`
	Mode             string   `json:"mode"`
	RestartRequired  bool     `json:"restart_required"`
	ResetProfile     bool     `json:"reset_profile"`
	ServerProbeError string   `json:"server_probe_error"`
	Purpose          string   `json:"purpose"`
	TargetURL        string   `json:"target_url"`
	CaptureID        string   `json:"capture_id"`
	CaptureRequired  bool     `json:"capture_required"`
	LoginOnly        bool     `json:"login_only"`
	AutomaticVisit   bool     `json:"automatic_visit"`
	ProviderRequest  bool     `json:"provider_request"`
	Interactive      bool     `json:"interactive"`
	FlowTokenID      *int     `json:"flow_token_id"`
	ProxyURL         string   `json:"proxy_url"`
	Runtime          string   `json:"runtime"`
	CookiePageHosts  []string `json:"cookie_page_hosts"`
	CookieDomains    []string `json:"cookie_domains"`
	CookieNames      []string `json:"cookie_names"`
}

type InboxFile struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	MTime int64  `json:"mtime"`
}

type HeartbeatResponse struct {
	PollSeconds        int           `json:"poll_seconds"`
	AgentVersion       string        `json:"agent_version"`
	UpdateRequired     bool          `json:"update_required"`
	Slots              []DesiredSlot `json:"slots"`
	InboxFiles         []InboxFile   `json:"inbox_files"`
	BindingEnrollments []Config      `json:"binding_enrollments,omitempty"`
}

type slotRuntime struct {
	mu              sync.Mutex
	spec            DesiredSlot
	chrome          *exec.Cmd
	ssh             *exec.Cmd
	connected       bool
	browser         string
	err             string
	stopping        bool
	authStatus      string
	accountName     string
	pageURL         string
	lastAuthCheck   time.Time
	authChecking    bool
	flowStatus      string
	flowCaptureID   string
	flowSubmitted   bool
	flowDiagnostics map[string]any
	lastFlowSubmit  time.Time
	profileReset    bool
	profilePath     string
	debugPort       int
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

type flowSessionProbe struct {
	Status             string                 `json:"status"`
	PageURL            string                 `json:"page_url"`
	SessionToken       string                 `json:"session_token"`
	SessionTokens      []string               `json:"session_tokens,omitempty"`
	Fingerprint        map[string]string      `json:"fingerprint"`
	SessionDiagnostics map[string]any         `json:"session_diagnostics,omitempty"`
	SessionCookies     []browserSessionCookie `json:"session_cookies,omitempty"`
}

type browserSessionCookie struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Secure   bool    `json:"secure"`
	HTTPOnly bool    `json:"http_only"`
	Expires  float64 `json:"expires"`
}

type slotRetryState struct {
	Failures int
	NextAt   time.Time
}

type agent struct {
	config      Config
	root        string
	profileRoot string
	inbox       string
	privateKey  string
	publicKey   string
	hostID      string
	httpClient  *http.Client
	mu          sync.Mutex
	slots       map[string]*slotRuntime
	// dormant slots retain their exact profile identity but intentionally have
	// no Chrome/SSH process while an API-only stage is running.
	dormant map[string]DesiredSlot
	// A server restart request is level-triggered and can remain true while
	// Chrome is still starting. Remember that it was consumed so heartbeats do
	// not repeatedly kill the same recovery attempt.
	serverRestartSeen map[string]bool
	retryMu           sync.Mutex
	retries           map[string]slotRetryState
	retryCaptureIDs   map[string]string
	syncMu            sync.Mutex
	syncedFiles       []InboxFile
	lastSyncAt        string
	syncError         string
	updateState       string
	updateError       string
}

var processUpdateScheduled atomic.Bool
var bindingMutationMu sync.Mutex
var runningBindings sync.Map
var profileRecoveryMu sync.Mutex

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
	bindings, err := loadInstalledBindings()
	if err != nil {
		writeFallbackLog(err)
		return
	}
	if err := cleanupLegacyInbox(os.Getenv("LOCALAPPDATA")); err != nil {
		writeFallbackLog(fmt.Errorf("legacy inbox cleanup warning: %w", err))
	}
	hostID, err := loadOrCreateHostID(bridgeInstallRoot(os.Getenv("LOCALAPPDATA")))
	if err != nil {
		writeFallbackLog(err)
		return
	}
	for _, binding := range bindings {
		if err := startInstalledBinding(binding, hostID); err != nil {
			writeFallbackLog(err)
		}
	}
	select {}
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
	root := bridgeInstallRoot(local)
	if err := os.MkdirAll(root, 0700); err != nil {
		return err
	}
	target := filepath.Join(root, "MYUPONA-HermesBridge.exe")
	// A legacy single-binding Agent may still own its Profile files. Stop the
	// one device host before migrating them; the new host restarts below with
	// every preserved binding.
	killExistingAgents(target)
	time.Sleep(250 * time.Millisecond)
	if err := migrateLegacyBinding(root); err != nil {
		return err
	}
	if err := assignRuntimePortBase(root, &config); err != nil {
		return err
	}
	if !samePath(exe, target) {
		if err := os.WriteFile(target, data, 0700); err != nil {
			return err
		}
	}
	bindingRoot := bridgeBindingRoot(root, config)
	if err := os.MkdirAll(bindingRoot, 0700); err != nil {
		return err
	}
	if err := writeConfig(filepath.Join(bindingRoot, "agent.json"), config); err != nil {
		return err
	}
	removeLegacyTasks()
	cleanupLegacyBridgeProcesses()
	runValue := fmt.Sprintf("\"%s\" --run", target)
	cmd := hiddenCommand("reg.exe", "add", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, "/v", "MYUPONA Hermes Bridge Agent", "/t", "REG_SZ", "/d", runValue, "/f")
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("register startup: %w: %s", err, strings.TrimSpace(string(output)))
	}
	killExistingAgents(target)
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

func bridgeInstallRoot(local string) string {
	return filepath.Join(local, "MYUPONA", "HermesBridgeAgent")
}

func bindingIdentity(config Config) string {
	value := fmt.Sprintf("%d:%d:%s", config.WorkspaceID, config.UserID, strings.TrimSpace(config.DeviceID))
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:8])
}

func bridgeBindingRoot(root string, config Config) string {
	return filepath.Join(root, "bindings", bindingIdentity(config))
}

func readConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	var config Config
	if err := json.Unmarshal(data, &config); err != nil {
		return Config{}, err
	}
	if config.APIURL == "" || config.Token == "" || config.DeviceID == "" {
		return Config{}, errors.New("installed binding configuration is incomplete")
	}
	return config, nil
}

func writeConfig(path string, config Config) error {
	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return err
	}
	temp := path + ".tmp"
	if err := os.WriteFile(temp, data, 0600); err != nil {
		return err
	}
	return os.Rename(temp, path)
}

func loadOrCreateHostID(root string) (string, error) {
	if err := os.MkdirAll(root, 0700); err != nil {
		return "", err
	}
	path := filepath.Join(root, "host-id")
	if data, err := os.ReadFile(path); err == nil {
		value := strings.TrimSpace(string(data))
		if matched, _ := regexp.MatchString(`^[a-f0-9]{32}$`, value); matched {
			return value, nil
		}
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return "", err
	}
	value := hex.EncodeToString(random)
	temp := path + ".tmp"
	if err := os.WriteFile(temp, []byte(value+"\n"), 0600); err != nil {
		return "", err
	}
	if err := os.Rename(temp, path); err != nil {
		return "", err
	}
	return value, nil
}

func installedBindingIDs() []string {
	bindings, err := loadInstalledBindings()
	if err != nil {
		return nil
	}
	ids := make([]string, 0, len(bindings))
	for _, binding := range bindings {
		ids = append(ids, bindingIdentity(binding.config))
	}
	slices.Sort(ids)
	return ids
}

func startInstalledBinding(binding installedBinding, hostID string) error {
	identity := bindingIdentity(binding.config)
	if _, loaded := runningBindings.LoadOrStore(identity, true); loaded {
		return nil
	}
	a, err := newAgent(binding.config, binding.root, hostID)
	if err != nil {
		runningBindings.Delete(identity)
		return fmt.Errorf("load binding %s: %w", binding.config.DeviceID, err)
	}
	a.logf("agent binding started version=%s workspace=%d user=%d device=%s host=%s", agentVersion, a.config.WorkspaceID, a.config.UserID, a.config.DeviceID, hostID)
	go a.run()
	return nil
}

func installBindingEnrollment(config Config, hostID string) error {
	if config.APIURL == "" || config.Token == "" || config.WorkspaceID < 1 || config.UserID < 1 || strings.TrimSpace(config.DeviceID) == "" {
		return errors.New("server returned an incomplete Bridge binding enrollment")
	}
	bindingMutationMu.Lock()
	defer bindingMutationMu.Unlock()
	root := bridgeInstallRoot(os.Getenv("LOCALAPPDATA"))
	if err := assignRuntimePortBase(root, &config); err != nil {
		return err
	}
	bindingRoot := bridgeBindingRoot(root, config)
	if err := os.MkdirAll(bindingRoot, 0700); err != nil {
		return err
	}
	path := filepath.Join(bindingRoot, "agent.json")
	if existing, err := readConfig(path); err == nil {
		// Refresh an expired credential without replacing the Profile or port
		// allocation owned by this exact workspace/user/device binding.
		config.RuntimePortBase = existing.RuntimePortBase
	}
	if err := writeConfig(path, config); err != nil {
		return err
	}
	return startInstalledBinding(installedBinding{config: config, root: bindingRoot}, hostID)
}

func migrateLegacyBinding(root string) error {
	legacyPath := filepath.Join(root, "agent.json")
	config, err := readConfig(legacyPath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read legacy Bridge binding: %w", err)
	}
	if err := assignRuntimePortBase(root, &config); err != nil {
		return err
	}
	targetRoot := bridgeBindingRoot(root, config)
	if err := os.MkdirAll(targetRoot, 0700); err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(targetRoot, "agent.json")); os.IsNotExist(err) {
		if err := writeConfig(filepath.Join(targetRoot, "agent.json"), config); err != nil {
			return err
		}
	}
	// Browser Profiles belong to the physical Windows host and fixed slot, not
	// to one logical binding. Moving the whole Profile store into the first
	// binding strands every other slot in a newly-created empty directory.
	// Keep the stores at the host root; startSlot performs guarded per-slot
	// recovery for clients that already ran the affected build.
	for _, name := range []string{
		"agent_ed25519", "agent_ed25519.pub", "known_hosts", "agent.log", "update-status.json",
	} {
		source := filepath.Join(root, name)
		target := filepath.Join(targetRoot, name)
		if _, statErr := os.Stat(source); statErr != nil {
			continue
		}
		if _, statErr := os.Stat(target); statErr == nil {
			continue
		}
		if renameErr := os.Rename(source, target); renameErr != nil {
			return fmt.Errorf("migrate legacy Bridge data %s: %w", name, renameErr)
		}
	}
	return os.Remove(legacyPath)
}

func assignRuntimePortBase(root string, config *Config) error {
	if config == nil {
		return errors.New("binding configuration is required")
	}
	bindingsRoot := filepath.Join(root, "bindings")
	entries, err := os.ReadDir(bindingsRoot)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	used := map[int]bool{}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		existing, readErr := readConfig(filepath.Join(bindingsRoot, entry.Name(), "agent.json"))
		if readErr != nil {
			continue
		}
		if bindingIdentity(existing) == bindingIdentity(*config) && existing.RuntimePortBase >= 20000 {
			config.RuntimePortBase = existing.RuntimePortBase
			return nil
		}
		if existing.RuntimePortBase >= 20000 {
			used[existing.RuntimePortBase] = true
		}
	}
	if config.RuntimePortBase >= 20000 && !used[config.RuntimePortBase] {
		return nil
	}
	for base := 20000; base <= 60160; base += 256 {
		if !used[base] {
			config.RuntimePortBase = base
			return nil
		}
	}
	return errors.New("this Windows device has no free Bridge runtime port block")
}

func loadInstalledBindings() ([]installedBinding, error) {
	local := os.Getenv("LOCALAPPDATA")
	if local == "" {
		return nil, errors.New("LOCALAPPDATA is not available")
	}
	root := bridgeInstallRoot(local)
	if err := migrateLegacyBinding(root); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(filepath.Join(root, "bindings"))
	if err != nil {
		return nil, err
	}
	bindings := make([]installedBinding, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		bindingRoot := filepath.Join(root, "bindings", entry.Name())
		config, readErr := readConfig(filepath.Join(bindingRoot, "agent.json"))
		if readErr != nil {
			continue
		}
		if config.RuntimePortBase < 20000 {
			if assignErr := assignRuntimePortBase(root, &config); assignErr != nil {
				return nil, assignErr
			}
			if writeErr := writeConfig(filepath.Join(bindingRoot, "agent.json"), config); writeErr != nil {
				return nil, writeErr
			}
		}
		bindings = append(bindings, installedBinding{config: config, root: bindingRoot})
	}
	if len(bindings) == 0 {
		return nil, errors.New("no installed Bridge bindings were found; download the client from GMV Ops")
	}
	return bindings, nil
}

func readUpdateStatus(root string) updateStatus {
	data, err := os.ReadFile(filepath.Join(root, "update-status.json"))
	if err != nil {
		return updateStatus{}
	}
	var status updateStatus
	_ = json.Unmarshal(data, &status)
	return status
}

func writeUpdateStatus(root string, status updateStatus) {
	data, _ := json.Marshal(status)
	_ = os.WriteFile(filepath.Join(root, "update-status.json"), data, 0600)
}

func newAgent(config Config, root string, hostID string) (*agent, error) {
	local := os.Getenv("LOCALAPPDATA")
	if config.LocalCapacity < 1 {
		config.LocalCapacity = 4
	}
	if config.ProfileCapacity < 1 {
		config.ProfileCapacity = 64
	}
	if config.ProfileCapacity > 128 {
		config.ProfileCapacity = 128
	}
	inbox := accountInboxRoot(local, config)
	if err := os.MkdirAll(inbox, 0700); err != nil {
		return nil, err
	}
	a := &agent{
		config:            config,
		root:              root,
		profileRoot:       bridgeInstallRoot(local),
		inbox:             inbox,
		privateKey:        filepath.Join(root, "agent_ed25519"),
		hostID:            hostID,
		httpClient:        &http.Client{Timeout: 45 * time.Second},
		slots:             make(map[string]*slotRuntime),
		dormant:           make(map[string]DesiredSlot),
		serverRestartSeen: make(map[string]bool),
		retries:           make(map[string]slotRetryState),
		retryCaptureIDs:   make(map[string]string),
	}
	status := readUpdateStatus(root)
	a.updateState = status.State
	a.updateError = status.Error
	if a.updateState == "" || a.updateState == "installed" || a.updateState == "installing" {
		a.updateState = "current"
		a.updateError = ""
		writeUpdateStatus(root, updateStatus{State: "current"})
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
		for _, enrollment := range response.BindingEnrollments {
			if err := installBindingEnrollment(enrollment, a.hostID); err != nil {
				a.logf("binding enrollment failed workspace=%d user=%d device=%s: %v", enrollment.WorkspaceID, enrollment.UserID, enrollment.DeviceID, err)
			}
		}
		if requiresAgentUpdate(agentVersion, response.AgentVersion, response.UpdateRequired) && time.Now().After(updateRetryAt) {
			targetVersion := strings.TrimSpace(response.AgentVersion)
			if !processUpdateScheduled.CompareAndSwap(false, true) {
				time.Sleep(time.Second)
				continue
			}
			a.updateState = "installing"
			a.updateError = ""
			writeUpdateStatus(a.root, updateStatus{State: "installing"})
			if err := a.installUpdate(targetVersion); err != nil {
				a.logf("agent update failed target=%s: %v", targetVersion, err)
				a.updateState = "failed"
				a.updateError = err.Error()
				writeUpdateStatus(a.root, updateStatus{State: "failed", Error: err.Error()})
				processUpdateScheduled.Store(false)
				updateRetryAt = time.Now().Add(5 * time.Minute)
			} else {
				a.logf("agent update scheduled current=%s target=%s", agentVersion, targetVersion)
				a.stopAll()
				time.Sleep(250 * time.Millisecond)
				os.Exit(0)
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
		DeviceID:          a.config.DeviceID,
		DeviceName:        a.config.DeviceName,
		AgentVersion:      agentVersion,
		PublicKey:         a.publicKey,
		InboxRoot:         a.inbox,
		LocalCapacity:     a.config.LocalCapacity,
		ProfileCapacity:   a.config.ProfileCapacity,
		HostID:            a.hostID,
		InstalledBindings: installedBindingIDs(),
		UpdateState:       a.updateState,
		UpdateError:       a.updateError,
		Slots:             a.statuses(),
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
			Purpose: slot.spec.Purpose, FlowStatus: slot.flowStatus, CaptureID: slot.flowCaptureID,
			SessionDiagnostics: slot.flowDiagnostics,
			ProfileReset:       slot.profileReset,
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
		a.resetSlotRetryForCapture(spec)
		if !a.slotRetryReady(id) {
			continue
		}
		running := &slotRuntime{spec: spec}
		a.slots[id] = running
		go a.startSlot(running)
	}
	a.mu.Unlock()
}

func directoryHasEntries(path string) bool {
	entries, err := os.ReadDir(path)
	return err == nil && len(entries) > 0
}

func profileStateScore(path string) int64 {
	if !directoryHasEntries(path) {
		return 0
	}
	var score int64 = 1
	seen := 0
	_ = filepath.WalkDir(path, func(current string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return nil
		}
		seen++
		if seen > 4096 {
			return filepath.SkipAll
		}
		info, statErr := entry.Info()
		if statErr == nil {
			score += info.Size()
		}
		name := strings.ToLower(filepath.ToSlash(current))
		if strings.HasSuffix(name, "/network/cookies") || strings.HasSuffix(name, "/cookies") {
			// Prefer the Profile that actually carries the durable browser cookie
			// store when an affected build left more than one non-empty candidate.
			score += 1 << 40
		}
		return nil
	})
	return score
}

func flowProfileRequiresExistingState(spec DesiredSlot) bool {
	if spec.Purpose != "flow_account" || spec.ResetProfile {
		return false
	}
	// A deliberate, visible first-login/re-login window may create a Profile.
	// Automatic maintenance, capture and provider work must never create and
	// silently use a blank Profile for an already-enrolled Flow account.
	return spec.AutomaticVisit || spec.CaptureRequired || spec.ProviderRequest || !spec.LoginOnly
}

func (a *agent) recoverHostBrowserProfile(spec DesiredSlot, storeName string, debugPort int) (string, string, error) {
	installRoot := strings.TrimSpace(a.profileRoot)
	if installRoot == "" {
		installRoot = bridgeInstallRoot(os.Getenv("LOCALAPPDATA"))
	}
	target := filepath.Join(installRoot, storeName, fmt.Sprintf("slot-%d", spec.LocalPort))

	profileRecoveryMu.Lock()
	defer profileRecoveryMu.Unlock()
	if directoryHasEntries(target) {
		return target, "", nil
	}

	candidateSet := map[string]bool{}
	addCandidate := func(path string) {
		cleaned := filepath.Clean(path)
		if !samePath(cleaned, target) {
			candidateSet[cleaned] = true
		}
	}
	addCandidate(filepath.Join(a.root, storeName, fmt.Sprintf("slot-%d", spec.LocalPort)))
	addCandidate(filepath.Join(a.root, storeName, spec.BridgeID))
	bindingsRoot := filepath.Join(installRoot, "bindings")
	if bindings, err := os.ReadDir(bindingsRoot); err == nil {
		for _, binding := range bindings {
			if !binding.IsDir() {
				continue
			}
			root := filepath.Join(bindingsRoot, binding.Name(), storeName)
			addCandidate(filepath.Join(root, fmt.Sprintf("slot-%d", spec.LocalPort)))
			addCandidate(filepath.Join(root, spec.BridgeID))
		}
	}

	best := ""
	var bestScore int64
	for candidate := range candidateSet {
		if score := profileStateScore(candidate); score > bestScore {
			best = candidate
			bestScore = score
		}
	}
	if best == "" {
		return target, "", nil
	}

	killBrowserRuntime(best, debugPort)
	killBrowserRuntime(target, debugPort)
	if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
		return "", "", err
	}
	quarantine := ""
	if _, err := os.Stat(target); err == nil {
		quarantine = fmt.Sprintf("%s.empty-%d", target, time.Now().UnixNano())
		if err := os.Rename(target, quarantine); err != nil {
			return "", "", fmt.Errorf("quarantine empty browser Profile: %w", err)
		}
	}
	if err := os.Rename(best, target); err != nil {
		if quarantine != "" {
			_ = os.Rename(quarantine, target)
		}
		return "", "", fmt.Errorf("restore browser Profile from %s: %w", best, err)
	}
	return target, best, nil
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
	storeName := "profiles"
	if isDoubaoDesktopRuntime(spec) {
		storeName = "doubao-app-profiles"
	}
	debugPort := a.runtimeDebugPort(spec.LocalPort)
	profileRoot := strings.TrimSpace(a.profileRoot)
	if profileRoot == "" {
		profileRoot = bridgeInstallRoot(os.Getenv("LOCALAPPDATA"))
	}
	profile := filepath.Join(profileRoot, storeName, fmt.Sprintf("slot-%d", spec.LocalPort))
	if !spec.ResetProfile {
		var recoveredFrom string
		var err error
		profile, recoveredFrom, err = a.recoverHostBrowserProfile(spec, storeName, debugPort)
		if err != nil {
			slot.setError(err)
			return
		}
		if recoveredFrom != "" {
			a.logf("restored persisted host browser profile bridge=%s slot=%d", spec.BridgeID, spec.LocalPort)
		}
	}
	legacyProfile := filepath.Join(filepath.Dir(profile), spec.BridgeID)
	slot.mu.Lock()
	slot.profilePath = profile
	slot.debugPort = debugPort
	slot.mu.Unlock()
	if spec.ResetProfile {
		killBrowserRuntime(profile, debugPort)
		if err := os.RemoveAll(profile); err != nil {
			slot.setError(fmt.Errorf("reset retired browser profile: %w", err))
			return
		}
		slot.mu.Lock()
		slot.profileReset = true
		slot.mu.Unlock()
	}
	if _, err := os.Stat(profile); os.IsNotExist(err) {
		if _, legacyErr := os.Stat(legacyProfile); legacyErr == nil {
			_ = os.Rename(legacyProfile, profile)
		}
	}
	if flowProfileRequiresExistingState(spec) && !directoryHasEntries(profile) {
		slot.setError(fmt.Errorf("existing Flow account Profile slot-%d is missing; refusing to create an empty Profile", spec.LocalPort))
		return
	}
	_ = os.MkdirAll(profile, 0700)
	if (spec.Purpose == "flow_account" || spec.Purpose == "jimeng_lab" || spec.Purpose == "doubao_lab" || spec.Purpose == "yt_dlp_account") && spec.LoginOnly {
		a.runAccountLoginSlot(slot, profile)
		return
	}
	cdpPort := 0
	if isDoubaoDesktopRuntime(spec) {
		killBrowserRuntime(profile, debugPort)
		appPath, err := doubaoDesktopPath()
		if err != nil {
			slot.setError(err)
			return
		}
		runtimeSpec := spec
		runtimeSpec.LocalPort = debugPort
		args := doubaoDesktopArguments(runtimeSpec, profile)
		var cmd *exec.Cmd
		if spec.Interactive || spec.LoginOnly {
			cmd = exec.Command(appPath, args...)
		} else {
			cmd = hiddenCommand(appPath, args...)
		}
		if err := cmd.Start(); err != nil {
			slot.setError(fmt.Errorf("start Doubao desktop: %w", err))
			return
		}
		slot.mu.Lock()
		slot.chrome = cmd
		slot.mu.Unlock()
		go cmd.Wait()
		if !waitCDP(debugPort, 45*time.Second) {
			slot.setError(errors.New("Doubao desktop CDP did not become ready; the app may enforce a single-instance profile"))
			return
		}
		cdpPort = debugPort
	} else if waitCDP(debugPort, 500*time.Millisecond) {
		// Compatibility with profiles created by older Agent versions.
		cdpPort = debugPort
	} else {
		cdpPort = waitProfileCDP(profile, 500*time.Millisecond)
	}
	if cdpPort == 0 {
		// Chrome 150 can ignore a fixed remote-debugging port while still
		// opening a normal browser window.  Use Chrome's secure ephemeral port
		// and discover it through DevToolsActivePort.  First remove every
		// process tied to this exact profile so an orphaned crashpad/renderer
		// cannot keep the profile lock and turn the next launch into a no-op.
		killBrowserRuntime(profile, debugPort)
		_ = os.Remove(filepath.Join(profile, "DevToolsActivePort"))
		_ = os.Remove(filepath.Join(profile, "lockfile"))
		chrome, err := chromePath()
		if err != nil {
			slot.setError(err)
			return
		}
		targetURL := strings.TrimSpace(spec.TargetURL)
		if targetURL == "" {
			targetURL = "https://chatgpt.com/"
		}
		chromeArgs := []string{
			"--remote-debugging-address=127.0.0.1",
			"--remote-debugging-port=0",
			"--remote-allow-origins=*",
			"--user-data-dir=" + profile,
			"--no-first-run", "--no-default-browser-check", "--new-window",
		}
		chromeArgs = append(chromeArgs, browserPresentationArguments(spec)...)
		if proxyArg := chromeProxyArgument(spec.ProxyURL); proxyArg != "" {
			chromeArgs = append(chromeArgs, proxyArg)
		}
		chromeArgs = append(chromeArgs, targetURL)
		var cmd *exec.Cmd
		if spec.Interactive {
			// Manual capture must be created on the logged-in user's visible
			// desktop. Windows HideWindow overrides Chrome window-state flags,
			// so an interactive diagnostic cannot use hiddenCommand even when
			// Browser.setWindowBounds later reports a normal window.
			cmd = exec.Command(chrome, chromeArgs...)
		} else {
			cmd = hiddenCommand(chrome, chromeArgs...)
		}
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
	if spec.Purpose == "flow_account" || spec.Purpose == "jimeng_lab" || spec.Purpose == "doubao_lab" || spec.Purpose == "yt_dlp_account" {
		slot.flowStatus = "checking"
		slot.flowCaptureID = spec.CaptureID
	}
	slot.mu.Unlock()
	if spec.Purpose == "flow_account" {
		go a.refreshFlowSession(slot, cdpPort)
	} else if spec.Purpose == "jimeng_lab" {
		go a.refreshJimengSession(slot, cdpPort)
	} else if spec.Purpose == "doubao_lab" {
		go a.refreshDoubaoSession(slot, cdpPort)
	} else if spec.Purpose == "yt_dlp_account" {
		go a.refreshYtDlpSession(slot, cdpPort)
	} else {
		go refreshSlotAuth(slot, cdpPort)
	}
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
			var probeErr error
			if spec.Purpose == "flow_account" {
				probeErr = a.refreshFlowSession(slot, cdpPort)
			} else if spec.Purpose == "jimeng_lab" {
				probeErr = a.refreshJimengSession(slot, cdpPort)
			} else if spec.Purpose == "doubao_lab" {
				probeErr = a.refreshDoubaoSession(slot, cdpPort)
			} else if spec.Purpose == "yt_dlp_account" {
				// The first page probe can race Chrome startup, redirects, or a
				// Douyin verification iframe. Keep probing the exact account slot
				// instead of falling through to the unrelated ChatGPT auth probe.
				probeErr = a.refreshYtDlpSession(slot, cdpPort)
			} else {
				var probe chatGPTAuthProbe
				probe, probeErr = probeChatGPTAuth(cdpPort)
				if probeErr == nil {
					slot.mu.Lock()
					slot.authStatus = probe.Status
					slot.accountName = probe.AccountName
					slot.pageURL = probe.PageURL
					slot.mu.Unlock()
				}
			}
			if waitCDP(cdpPort, 2*time.Second) && probeErr == nil {
				cdpFailures = 0
				slot.mu.Lock()
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
			killBrowserRuntime(profile, debugPort)
			a.logf("slot watchdog restarting bridge=%s reason=%s", spec.BridgeID, message)
			return
		}
	}
}

func (a *agent) runAccountLoginSlot(slot *slotRuntime, profile string) {
	spec := slot.spec
	debugPort := a.runtimeDebugPort(spec.LocalPort)
	slot.mu.Lock()
	slot.profilePath = profile
	slot.debugPort = debugPort
	slot.mu.Unlock()
	killBrowserRuntime(profile, debugPort)
	chrome, err := chromePath()
	if err != nil {
		slot.setError(err)
		return
	}
	targetURL := strings.TrimSpace(spec.TargetURL)
	if targetURL == "" {
		targetURL = "https://labs.google/fx/tools/flow"
	}
	// Google rejects sign-in from a browser launched with remote-debugging.
	// Login therefore happens in a completely normal Chrome. After the user
	// closes it, the server advances this same profile into a short CDP capture.
	chromeArgs := []string{
		"--user-data-dir=" + profile,
		"--no-first-run", "--no-default-browser-check", "--disable-background-mode", "--new-window",
	}
	if spec.AutomaticVisit {
		// This is a normal (non-CDP, non-headless) Flow visit using the exact
		// account Profile and proxy. Starting minimized avoids stealing focus.
		// Explicit anti-throttling switches are required because Flow refreshes
		// its page-owned grant from renderer work which Chrome would otherwise
		// defer while the window is minimized.
		chromeArgs = append(chromeArgs, automaticFlowVisitArguments()...)
	}
	if proxyArg := chromeProxyArgument(spec.ProxyURL); proxyArg != "" {
		chromeArgs = append(chromeArgs, proxyArg)
	}
	chromeArgs = append(chromeArgs, targetURL)
	var cmd *exec.Cmd
	if spec.AutomaticVisit {
		// Keep a real, renderer-backed Chrome window. HideWindow can prevent
		// Flow's page-owned grant bootstrap from running for older accounts;
		// --start-minimized avoids stealing focus while preserving rendering.
		cmd = exec.Command(chrome, chromeArgs...)
	} else {
		cmd = hiddenCommand(chrome, chromeArgs...)
	}
	if err := cmd.Start(); err != nil {
		slot.setError(fmt.Errorf("start normal Chrome login window: %w", err))
		return
	}
	slot.mu.Lock()
	slot.chrome = cmd
	slot.connected = false
	slot.browser = "Chrome"
	slot.flowStatus = "login_required"
	slot.flowCaptureID = spec.CaptureID
	slot.pageURL = targetURL
	slot.err = ""
	slot.mu.Unlock()
	a.logf("account login window opened purpose=%s bridge=%s profile_slot=%d automatic=%t", spec.Purpose, spec.BridgeID, spec.LocalPort, spec.AutomaticVisit)
	if spec.AutomaticVisit {
		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()
		select {
		case <-time.After(60 * time.Second):
			// Match the successful manual lifecycle: ask the main window to close
			// normally so Chrome flushes rotated Flow cookies to this Profile.
			// A forced process kill here used to discard the newly refreshed grant,
			// making the following capture falsely report login_required.
			if !closeBrowserRuntimeGracefully(profile, debugPort, 12*time.Second) {
				killBrowserRuntime(profile, debugPort)
			}
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				if cmd.Process != nil {
					_ = cmd.Process.Kill()
				}
			}
		case <-done:
		}
	} else {
		_ = cmd.Wait()
	}
	slot.mu.Lock()
	slot.chrome = nil
	if !slot.stopping {
		slot.flowStatus = "login_complete"
		slot.err = ""
	}
	slot.mu.Unlock()
	// Keep the acknowledgement stable instead of reopening Chrome on every
	// heartbeat. The server's login_complete transition changes LoginOnly and
	// reconcile then restarts this exact profile in capture mode.
	for {
		time.Sleep(250 * time.Millisecond)
		slot.mu.Lock()
		stopping := slot.stopping
		slot.mu.Unlock()
		if stopping {
			return
		}
	}
}

func automaticFlowVisitArguments() []string {
	return []string{
		"--start-minimized",
		"--disable-session-crashed-bubble",
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
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
	killBrowserRuntime(s.profilePath, s.debugPort)
	s.connected = false
}

func (a *agent) runtimeDebugPort(localPort int) int {
	base := a.config.RuntimePortBase
	if base < 20000 {
		base = 20000
	}
	offset := localPort % 256
	if offset < 0 {
		offset = -offset
	}
	return base + offset
}

func killBrowserRuntime(profile string, port int) {
	if port <= 0 && strings.TrimSpace(profile) == "" {
		return
	}
	// Chrome may outlive the process handle that originally launched it (for
	// example after an agent auto-update). Match only this slot's debugging
	// port so recovering one project cannot close another project's browser.
	pattern := fmt.Sprintf(`--remote-debugging-port(?:=|\s+)%d(?:\s|$)`, port)
	profilePattern := regexp.QuoteMeta(profile)
	if profilePattern == "" {
		profilePattern = "(?!)"
	}
	script := fmt.Sprintf(`
$pattern = '%s'
$profilePattern = '%s'
Get-CimInstance Win32_Process |
  Where-Object {
    ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe' -or $_.Name -match '(?i)doubao|豆包') -and
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

func closeBrowserRuntimeGracefully(profile string, port int, timeout time.Duration) bool {
	if port <= 0 && strings.TrimSpace(profile) == "" {
		return true
	}
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	pattern := fmt.Sprintf(`--remote-debugging-port(?:=|\s+)%d(?:\s|$)`, port)
	profilePattern := regexp.QuoteMeta(profile)
	if profilePattern == "" {
		profilePattern = "(?!)"
	}
	timeoutMillis := timeout.Milliseconds()
	if timeoutMillis < 1000 {
		timeoutMillis = 1000
	}
	// Only the browser root process has a top-level window. CloseMainWindow is
	// the Windows equivalent of the user pressing the window close button and
	// lets Chrome persist its cookie database. Child processes are allowed to
	// exit naturally; the caller retains a fixed-profile forced-kill fallback.
	script := fmt.Sprintf(`
$pattern = '%s'
$profilePattern = '%s'
$deadline = [DateTime]::UtcNow.AddMilliseconds(%d)
$targets = Get-CimInstance Win32_Process | Where-Object {
  ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and
  ($_.CommandLine -match $pattern -or $_.CommandLine -match $profilePattern)
}
foreach ($target in $targets) {
  try {
    $process = Get-Process -Id $target.ProcessId -ErrorAction Stop
    if ($process.MainWindowHandle -ne 0) { [void]$process.CloseMainWindow() }
  } catch {}
}
do {
  Start-Sleep -Milliseconds 200
  $remaining = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and
    ($_.CommandLine -match $pattern -or $_.CommandLine -match $profilePattern)
  }
} while ($remaining -and [DateTime]::UtcNow -lt $deadline)
if ($remaining) { exit 2 }
exit 0
`, strings.ReplaceAll(pattern, "'", "''"), strings.ReplaceAll(profilePattern, "'", "''"), timeoutMillis)
	return hiddenCommand(
		"powershell.exe",
		"-NoProfile",
		"-ExecutionPolicy", "Bypass",
		"-WindowStyle", "Hidden",
		"-Command", script,
	).Run() == nil
}

func (a *agent) slotRetryReady(bridgeID string) bool {
	a.retryMu.Lock()
	defer a.retryMu.Unlock()
	state, ok := a.retries[bridgeID]
	return !ok || !time.Now().Before(state.NextAt)
}

func (a *agent) resetSlotRetryForCapture(spec DesiredSlot) {
	if spec.Purpose != "flow_account" && spec.Purpose != "jimeng_lab" && spec.Purpose != "doubao_lab" {
		return
	}
	captureID := strings.TrimSpace(spec.CaptureID)
	if captureID == "" {
		return
	}
	a.retryMu.Lock()
	defer a.retryMu.Unlock()
	if a.retryCaptureIDs == nil {
		a.retryCaptureIDs = make(map[string]string)
	}
	if a.retryCaptureIDs[spec.BridgeID] == captureID {
		return
	}
	// A new server-owned capture cycle is an independent idempotent attempt.
	// It must not inherit a 15-minute local browser backoff from an older cycle.
	delete(a.retries, spec.BridgeID)
	a.retryCaptureIDs[spec.BridgeID] = captureID
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
  const visible = (node) => {
    if (!node) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const loginControl = [...document.querySelectorAll('a,button,[role="button"]')]
    .filter(visible)
    .some((node) => /^(log in|sign in|登录|登入)$/i.test(
      ((node.innerText || node.textContent || '') + ' ' + (node.getAttribute('aria-label') || '')).trim()
    ));
  const bootstrapLoggedIn = bootstrap.includes('logged_in');
  const bootstrapLoggedOut = bootstrap.includes('logged_out');
  const loginBanner = /log in to (?:get|receive)|sign in to (?:get|receive)|登录以获取|登录后.*(?:创建图片|上传文件)/i.test(body.slice(0, 2400));
  // ChatGPT's hydration payload can retain both logged_in and logged_out
  // strings, and the anonymous page still has a generic profile/menu button
  // plus a text composer. Visible login UI wins over those stale hints.
  const loggedOut = loginControl || loginBanner || (bootstrapLoggedOut && !bootstrapLoggedIn);
  const loggedIn = !loggedOut && (bootstrapLoggedIn || Boolean(accountButton));
  let status = 'checking';
  if (loggedOut) status = 'login_required';
  else if (loggedIn && composer) status = 'ready';
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

func (a *agent) refreshFlowSession(slot *slotRuntime, port int) error {
	probe, err := probeFlowSession(port)
	if err != nil {
		return err
	}
	slot.mu.Lock()
	slot.pageURL = probe.PageURL
	slot.flowStatus = probe.Status
	slot.flowCaptureID = slot.spec.CaptureID
	slot.flowDiagnostics = probe.SessionDiagnostics
	shouldSubmit := slot.spec.CaptureRequired && probe.Status == "ready" && probe.SessionToken != "" &&
		!slot.flowSubmitted && (slot.lastFlowSubmit.IsZero() || time.Since(slot.lastFlowSubmit) >= 30*time.Second)
	if shouldSubmit {
		slot.lastFlowSubmit = time.Now()
		slot.flowStatus = "capturing"
	}
	slot.mu.Unlock()
	if !shouldSubmit {
		return nil
	}

	payload := map[string]any{
		"device_id":           a.config.DeviceID,
		"bridge_id":           slot.spec.BridgeID,
		"capture_id":          slot.spec.CaptureID,
		"session_token":       probe.SessionToken,
		"session_tokens":      probe.SessionTokens,
		"session_diagnostics": probe.SessionDiagnostics,
		"profile_id":          fmt.Sprintf("%s/slot-%d", a.config.DeviceID, slot.spec.LocalPort),
		"fingerprint":         probe.Fingerprint,
	}
	accepted, submitErr := a.submitFlowCapture(payload)
	slot.mu.Lock()
	defer slot.mu.Unlock()
	if submitErr != nil {
		slot.flowStatus = "ready"
		return nil
	}
	slot.flowSubmitted = true
	if accepted {
		slot.flowStatus = "submitted"
	} else {
		// The server persisted either a bounded retry or a terminal validation
		// state and will omit this slot on the next heartbeat.
		slot.flowStatus = "submitted"
	}
	return nil
}

func (a *agent) refreshJimengSession(slot *slotRuntime, port int) error {
	probe, err := probeJimengSession(port)
	if err != nil {
		return err
	}
	slot.mu.Lock()
	slot.pageURL = probe.PageURL
	slot.flowStatus = probe.Status
	slot.flowCaptureID = slot.spec.CaptureID
	shouldSubmit := slot.spec.CaptureRequired && probe.Status == "ready" && probe.SessionToken != "" &&
		!slot.flowSubmitted && (slot.lastFlowSubmit.IsZero() || time.Since(slot.lastFlowSubmit) >= 30*time.Second)
	if shouldSubmit {
		slot.lastFlowSubmit = time.Now()
		slot.flowStatus = "capturing"
	}
	slot.mu.Unlock()
	if !shouldSubmit {
		return nil
	}
	payload := map[string]any{
		"device_id":           a.config.DeviceID,
		"bridge_id":           slot.spec.BridgeID,
		"capture_id":          slot.spec.CaptureID,
		"session_token":       probe.SessionToken,
		"session_tokens":      probe.SessionTokens,
		"session_diagnostics": probe.SessionDiagnostics,
		"session_cookies":     probe.SessionCookies,
		"profile_id":          fmt.Sprintf("%s/slot-%d", a.config.DeviceID, slot.spec.LocalPort),
		"fingerprint":         probe.Fingerprint,
	}
	accepted, submitErr := a.submitAccountCapture("/jimeng/capture", payload)
	slot.mu.Lock()
	defer slot.mu.Unlock()
	if submitErr != nil {
		slot.flowStatus = "ready"
		return nil
	}
	slot.flowSubmitted = true
	if accepted {
		slot.flowStatus = "submitted"
	} else {
		slot.flowStatus = "submitted"
	}
	return nil
}

func (a *agent) refreshDoubaoSession(slot *slotRuntime, port int) error {
	probe, err := probeDoubaoSession(port)
	if err != nil {
		return err
	}
	slot.mu.Lock()
	slot.pageURL = probe.PageURL
	slot.flowStatus = probe.Status
	slot.flowCaptureID = slot.spec.CaptureID
	shouldSubmit := slot.spec.CaptureRequired && probe.Status == "ready" && probe.SessionToken != "" &&
		!slot.flowSubmitted && (slot.lastFlowSubmit.IsZero() || time.Since(slot.lastFlowSubmit) >= 30*time.Second)
	if shouldSubmit {
		slot.lastFlowSubmit = time.Now()
		slot.flowStatus = "capturing"
	}
	slot.mu.Unlock()
	if !shouldSubmit {
		return nil
	}
	payload := map[string]any{
		"device_id":           a.config.DeviceID,
		"bridge_id":           slot.spec.BridgeID,
		"capture_id":          slot.spec.CaptureID,
		"session_token":       probe.SessionToken,
		"session_tokens":      probe.SessionTokens,
		"session_diagnostics": probe.SessionDiagnostics,
		"session_cookies":     probe.SessionCookies,
		"profile_id":          fmt.Sprintf("%s/slot-%d", a.config.DeviceID, slot.spec.LocalPort),
		"fingerprint":         probe.Fingerprint,
	}
	accepted, submitErr := a.submitAccountCapture("/doubao/capture", payload)
	slot.mu.Lock()
	defer slot.mu.Unlock()
	if submitErr != nil {
		slot.flowStatus = "ready"
		return nil
	}
	slot.flowSubmitted = true
	if accepted {
		slot.flowStatus = "submitted"
	} else {
		slot.flowStatus = "submitted"
	}
	return nil
}

func (a *agent) refreshYtDlpSession(slot *slotRuntime, port int) error {
	probe, err := probeYtDlpSession(port, slot.spec)
	if err != nil {
		return err
	}
	slot.mu.Lock()
	slot.pageURL = probe.PageURL
	slot.flowStatus = probe.Status
	slot.flowCaptureID = slot.spec.CaptureID
	shouldSubmit := slot.spec.CaptureRequired && probe.Status == "ready" && len(probe.SessionCookies) > 0 &&
		!slot.flowSubmitted && (slot.lastFlowSubmit.IsZero() || time.Since(slot.lastFlowSubmit) >= 30*time.Second)
	if shouldSubmit {
		slot.lastFlowSubmit = time.Now()
		slot.flowStatus = "capturing"
	}
	slot.mu.Unlock()
	if !shouldSubmit {
		return nil
	}
	payload := map[string]any{
		"device_id":       a.config.DeviceID,
		"bridge_id":       slot.spec.BridgeID,
		"capture_id":      slot.spec.CaptureID,
		"session_cookies": probe.SessionCookies,
		"profile_id":      fmt.Sprintf("%s/slot-%d", a.config.DeviceID, slot.spec.LocalPort),
	}
	accepted, submitErr := a.submitAccountCapture("/yt-dlp/capture", payload)
	slot.mu.Lock()
	defer slot.mu.Unlock()
	if submitErr != nil {
		slot.flowStatus = "ready"
		return nil
	}
	slot.flowSubmitted = true
	if accepted {
		slot.flowStatus = "submitted"
	} else {
		slot.flowStatus = "submitted"
	}
	return nil
}

func (a *agent) submitFlowCapture(payload map[string]any) (bool, error) {
	return a.submitAccountCapture("/flow/capture", payload)
}

func (a *agent) submitAccountCapture(path string, payload map[string]any) (bool, error) {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false, err
	}
	req, err := http.NewRequest(http.MethodPost, a.config.APIURL+path, bytes.NewReader(encoded))
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", "Bearer "+a.config.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("account capture returned %s", resp.Status)
	}
	var result struct {
		Success bool `json:"success"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return false, err
	}
	return result.Success, nil
}

func probeFlowSession(port int) (flowSessionProbe, error) {
	return probeWebSession(
		port,
		[]string{"labs.google"},
		[]string{"labs.google"},
		[]string{"__Secure-next-auth.session-token"},
		nil,
	)
}

func probeJimengSession(port int) (flowSessionProbe, error) {
	return probeWebSession(
		port,
		[]string{"jimeng.jianying.com", "dreamina.capcut.com"},
		[]string{"jianying.com", "capcut.com"},
		[]string{"sessionid", "sessionid_ss", "sid_tt"},
		nil,
	)
}

func probeYtDlpSession(port int, spec DesiredSlot) (flowSessionProbe, error) {
	if len(spec.CookiePageHosts) == 0 || len(spec.CookieDomains) == 0 || len(spec.CookieNames) == 0 {
		return flowSessionProbe{}, errors.New("yt-dlp cookie capture configuration is incomplete")
	}
	return probeWebSession(port, spec.CookiePageHosts, spec.CookieDomains, spec.CookieNames, nil)
}

func probeDoubaoSession(port int) (flowSessionProbe, error) {
	return probeWebSession(
		port,
		[]string{"www.doubao.com", "doubao.com"},
		[]string{"doubao.com"},
		[]string{"sessionid", "sessionid_ss", "sid_tt"},
		[]string{"doubao"},
	)
}

func domainAllowed(domain string, allowed []string) bool {
	domain = strings.ToLower(strings.TrimPrefix(strings.TrimSpace(domain), "."))
	for _, host := range allowed {
		host = strings.ToLower(strings.TrimPrefix(strings.TrimSpace(host), "."))
		if domain == host || strings.HasSuffix(domain, "."+host) {
			return true
		}
	}
	return false
}

func stringAllowed(value string, allowed []string) bool {
	value = strings.ToLower(strings.TrimSpace(value))
	for _, candidate := range allowed {
		if value == strings.ToLower(strings.TrimSpace(candidate)) {
			return true
		}
	}
	return false
}

func probeWebSession(port int, pageHosts []string, cookieDomains []string, cookieNames []string, internalSchemes []string) (flowSessionProbe, error) {
	probe := flowSessionProbe{
		Status: "checking", Fingerprint: map[string]string{}, SessionDiagnostics: map[string]any{},
	}
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
		parsed, parseErr := url.Parse(candidate.URL)
		matched := parseErr == nil && (domainAllowed(parsed.Hostname(), pageHosts) || stringAllowed(parsed.Scheme, internalSchemes))
		if candidate.Type == "page" && matched {
			page = candidate
			break
		}
	}
	if page.WebSocketDebuggerURL == "" {
		probe.Status = "login_required"
		return probe, nil
	}
	probe.PageURL = page.URL
	dialer := websocket.Dialer{HandshakeTimeout: 5 * time.Second}
	conn, _, err := dialer.Dial(page.WebSocketDebuggerURL, http.Header{"Origin": []string{"http://127.0.0.1"}})
	if err != nil {
		return probe, err
	}
	defer conn.Close()
	deadline := time.Now().Add(8 * time.Second)
	_ = conn.SetReadDeadline(deadline)
	_ = conn.SetWriteDeadline(deadline)
	expression := `JSON.stringify((() => {
	  const d = navigator.userAgentData;
	  const brands = Array.isArray(d?.brands) ? d.brands.map((b) => '"' + b.brand + '";v="' + b.version + '"').join(', ') : '';
	  const deviceParams = {};
	  for (const entry of performance.getEntriesByType('resource').slice(-300)) {
	    try {
	      const u = new URL(entry.name);
	      for (const [target, names] of Object.entries({device_id:['device_id','deviceId'], web_id:['web_id','webId'], fp:['fp','verifyFp']})) {
	        if (deviceParams[target]) continue;
	        for (const name of names) {
	          const value = u.searchParams.get(name);
	          if (value && value.length <= 256) { deviceParams[target] = value; break; }
	        }
	      }
	    } catch (_) {}
	  }
	  return {
    fingerprint: {
      user_agent: navigator.userAgent || '',
      accept_language: (navigator.languages || [navigator.language]).filter(Boolean).join(', '),
      sec_ch_ua: brands,
      sec_ch_ua_mobile: d?.mobile ? '?1' : '?0',
      sec_ch_ua_platform: d?.platform ? '"' + d.platform + '"' : '',
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || ''
    },
	    diagnostics: {
	      window_login_state: typeof window.__isLogined === 'boolean' ? window.__isLogined : null,
	      device_params: deviceParams,
      local_storage_keys: Object.keys(localStorage || {}).filter((key) => !/token|secret|session|cookie/i.test(key)).slice(0, 40),
      document_cookie_names: (document.cookie || '').split(';').map((item) => item.split('=', 1)[0].trim()).filter(Boolean).slice(0, 80)
    }
  };
})())`
	if err := conn.WriteJSON(map[string]any{
		"id": 1, "method": "Runtime.evaluate",
		"params": map[string]any{"expression": expression, "returnByValue": true, "awaitPromise": true},
	}); err != nil {
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
			return probe, errors.New("CDP fingerprint probe returned no value")
		}
		var pageProbe struct {
			Fingerprint map[string]string `json:"fingerprint"`
			Diagnostics map[string]any    `json:"diagnostics"`
		}
		if err := json.Unmarshal([]byte(value), &pageProbe); err != nil {
			return probe, err
		}
		probe.Fingerprint = pageProbe.Fingerprint
		probe.SessionDiagnostics = pageProbe.Diagnostics
		break
	}
	// Network.getAllCookies includes stale cookies from unrelated paths and
	// sibling hosts in the same persistent profile. Asking Chrome for cookies
	// applicable to the exact page mirrors what the browser would actually send
	// to JiMeng and avoids selecting an expired root-domain session by accident.
	cookieCommand := map[string]any{"id": 2, "method": "Network.getCookies", "params": map[string]any{"urls": []string{page.URL}}}
	for _, domain := range cookieDomains {
		if strings.EqualFold(strings.TrimSpace(domain), "google.com") {
			cookieCommand = map[string]any{"id": 2, "method": "Network.getCookies", "params": map[string]any{
				"urls": []string{page.URL, "https://www.youtube.com/", "https://accounts.google.com/", "https://google.com/"},
			}}
			break
		}
	}
	if parsed, parseErr := url.Parse(page.URL); parseErr == nil && stringAllowed(parsed.Scheme, internalSchemes) {
		// The desktop app renders a signed ``doubao://`` shell while its authenticated
		// requests still use doubao.com cookies. Query the same Chromium profile's
		// cookie jar, then retain only the allow-listed Doubao parent domains below.
		cookieCommand = map[string]any{"id": 2, "method": "Network.getAllCookies"}
	}
	if err := conn.WriteJSON(cookieCommand); err != nil {
		return probe, err
	}
	for {
		var message struct {
			ID     int `json:"id"`
			Result struct {
				Cookies []struct {
					Name     string  `json:"name"`
					Value    string  `json:"value"`
					Domain   string  `json:"domain"`
					Path     string  `json:"path"`
					Secure   bool    `json:"secure"`
					HTTPOnly bool    `json:"httpOnly"`
					Expires  float64 `json:"expires"`
				} `json:"cookies"`
			} `json:"result"`
		}
		if err := conn.ReadJSON(&message); err != nil {
			return probe, err
		}
		if message.ID != 2 {
			continue
		}
		// Keep a small, deterministic set of applicable candidates. JiMeng may
		// rotate one cookie variant before the others; the server verifies each
		// candidate through the account's fixed proxy and persists only the one
		// that is actually live.
		seen := map[string]bool{}
		cookieHints := make([]map[string]any, 0, len(message.Result.Cookies))
		for _, cookie := range message.Result.Cookies {
			domain := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(cookie.Domain)), ".")
			if !domainAllowed(domain, cookieDomains) {
				continue
			}
			if strings.TrimSpace(cookie.Name) != "" && strings.TrimSpace(cookie.Value) != "" {
				probe.SessionCookies = append(probe.SessionCookies, browserSessionCookie{
					Name: cookie.Name, Value: cookie.Value, Domain: domain, Path: cookie.Path,
					Secure: cookie.Secure, HTTPOnly: cookie.HTTPOnly, Expires: cookie.Expires,
				})
			}
			cookieHints = append(cookieHints, map[string]any{
				"name": cookie.Name, "domain": domain, "path": cookie.Path,
				"secure": cookie.Secure, "http_only": cookie.HTTPOnly,
				"expired": cookie.Expires > 0 && cookie.Expires < float64(time.Now().Unix()),
			})
			if len(cookieHints) >= 80 {
				break
			}
		}
		probe.SessionDiagnostics["applicable_cookies"] = cookieHints
		for _, wantedName := range cookieNames {
			for _, cookie := range message.Result.Cookies {
				domain := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(cookie.Domain)), ".")
				value := strings.TrimSpace(cookie.Value)
				if cookie.Name != wantedName || value == "" || !domainAllowed(domain, cookieDomains) || seen[value] {
					continue
				}
				seen[value] = true
				probe.SessionTokens = append(probe.SessionTokens, value)
				if len(probe.SessionTokens) >= 8 {
					break
				}
			}
			if len(probe.SessionTokens) >= 8 {
				break
			}
		}
		if len(probe.SessionTokens) > 0 {
			probe.SessionToken = probe.SessionTokens[0]
		}
		probe.SessionDiagnostics["candidate_count"] = len(probe.SessionTokens)
		break
	}
	if probe.SessionToken == "" {
		probe.Status = "login_required"
	} else {
		probe.Status = "ready"
	}
	return probe, nil
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

func isDoubaoDesktopRuntime(spec DesiredSlot) bool {
	return spec.Purpose == "doubao_lab" && strings.EqualFold(strings.TrimSpace(spec.Runtime), "doubao_desktop")
}

func doubaoDesktopArguments(spec DesiredSlot, profile string) []string {
	args := []string{
		"--remote-debugging-address=127.0.0.1",
		"--remote-debugging-port=" + strconv.Itoa(spec.LocalPort),
		"--remote-allow-origins=*",
		"--user-data-dir=" + profile,
		"--no-first-run",
	}
	if proxyArg := chromeProxyArgument(spec.ProxyURL); proxyArg != "" {
		args = append(args, proxyArg)
	}
	return args
}

func doubaoDesktopPath() (string, error) {
	candidates := []string{
		filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "Doubao", "Doubao.exe"),
		filepath.Join(os.Getenv("LOCALAPPDATA"), "Doubao", "Doubao.exe"),
		filepath.Join(os.Getenv("PROGRAMFILES"), "Doubao", "Doubao.exe"),
		filepath.Join(os.Getenv("PROGRAMFILES(X86)"), "Doubao", "Doubao.exe"),
	}
	for _, candidate := range candidates {
		if candidate != "" {
			if _, err := os.Stat(candidate); err == nil {
				return candidate, nil
			}
		}
	}
	script := `$process = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '(?i)doubao|豆包' -and $_.ExecutablePath } | Select-Object -First 1 -ExpandProperty ExecutablePath
if ($process) { $process; exit 0 }
$uninstall = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match '豆包|Doubao' -and $_.InstallLocation } | Select-Object -First 1
if ($uninstall) { Get-ChildItem -LiteralPath $uninstall.InstallLocation -Filter '*.exe' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(?i)doubao|豆包' } | Select-Object -First 1 -ExpandProperty FullName }`
	output, err := hiddenCommand(
		"powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script,
	).Output()
	if err == nil {
		candidate := strings.TrimSpace(string(output))
		if candidate != "" {
			if _, statErr := os.Stat(candidate); statErr == nil {
				return candidate, nil
			}
		}
	}
	return "", errors.New("Doubao desktop app was not found in the current Windows user profile")
}

func hiddenCommand(name string, args ...string) *exec.Cmd {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	return cmd
}

func chromeProxyArgument(raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Host == "" {
		return ""
	}
	if strings.EqualFold(parsed.Scheme, "socks5h") {
		parsed.Scheme = "socks5"
	}
	switch strings.ToLower(parsed.Scheme) {
	case "http", "https", "socks5":
		return "--proxy-server=" + parsed.String()
	default:
		return ""
	}
}

func browserPresentationArguments(spec DesiredSlot) []string {
	if (spec.Purpose == "flow_account" || spec.Purpose == "yt_dlp_account") && spec.CaptureRequired {
		// Flow grants and yt-dlp Cookie keepalives are automatic capture
		// probes. They must never create a visible browser window or steal
		// focus. Explicit login cycles use LoginOnly and are handled by
		// runAccountLoginSlot before reaching this path.
		return []string{"--headless=new", "--window-size=1280,900"}
	}
	if spec.Purpose == "doubao_lab" && spec.ProviderRequest {
		// Seedance applies a different risk boundary to headless Chromium even
		// when the same Profile and page-owned signed request work interactively.
		// Keep a real Chrome runtime but start it minimized so provider work does
		// not steal focus from the Windows user.
		if spec.Interactive {
			return []string{"--window-size=1360,900", "--window-position=80,60"}
		}
		return []string{"--window-size=1280,900", "--start-minimized"}
	}
	return nil
}

func sameSlot(left, right DesiredSlot) bool {
	return left.BridgeID == right.BridgeID && left.LocalPort == right.LocalPort && left.ServerPort == right.ServerPort &&
		left.SSHHost == right.SSHHost && left.SSHUser == right.SSHUser && left.SSHPort == right.SSHPort &&
		left.Purpose == right.Purpose && left.TargetURL == right.TargetURL && left.CaptureID == right.CaptureID &&
		left.CaptureRequired == right.CaptureRequired && left.LoginOnly == right.LoginOnly &&
		left.AutomaticVisit == right.AutomaticVisit &&
		left.ProviderRequest == right.ProviderRequest && left.Interactive == right.Interactive && left.ProxyURL == right.ProxyURL &&
		left.Runtime == right.Runtime && slices.Equal(left.CookiePageHosts, right.CookiePageHosts) &&
		slices.Equal(left.CookieDomains, right.CookieDomains) && slices.Equal(left.CookieNames, right.CookieNames)
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

func killExistingAgents(target string) {
	script := fmt.Sprintf(`
$current = $PID
$target = %s
Get-CimInstance Win32_Process |
  Where-Object {
    $_.ProcessId -ne $current -and
    $_.Name -eq 'MYUPONA-HermesBridge.exe' -and
    $_.CommandLine -like '*--run*' -and
    ($_.ExecutablePath -eq $target -or $_.ExecutablePath -like '*\\MYUPONA\\HermesBridgeAgent\\MYUPONA-HermesBridge.exe')
  } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
  }
`, psSingleQuote(target))
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
