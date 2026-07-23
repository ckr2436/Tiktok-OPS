package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
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
