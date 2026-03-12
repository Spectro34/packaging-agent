package osc

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type BranchPackageParam struct {
	Project       string `json:"project_name" jsonschema:"The project from which the package is branched."`
	Bundle        string `json:"bundle_name" jsonschema:"The bundle or source package that you want to branch or copy."`
	TargetProject string `json:"target_project,omitempty" jsonschema:"The target project to branch the package to. If not provided, a new project will be created."`
	Copy          bool   `json:"copy,omitempty" jsonschema:"Copy the package instead of branching."`
}

type BranchResult struct {
	TargetProject string `json:"target_project"`
	TargetPackage string `json:"target_package"`
	CheckoutDir   string `json:"checkout_dir"`
}

func (cred OSCCredentials) BranchBundle(ctx context.Context, req *mcp.CallToolRequest, params BranchPackageParam) (*mcp.CallToolResult, BranchResult, error) {
	slog.Debug("mcp tool call: BranchBundle", "session", req.Session.ID(), "params", params)
	if params.Project == "" {
		return nil, BranchResult{}, fmt.Errorf("project name cannot be empty")
	}
	if params.Bundle == "" {
		return nil, BranchResult{}, fmt.Errorf("package name cannot be empty")
	}

	targetProject := params.TargetProject
	if targetProject == "" {
		targetProject = fmt.Sprintf("home:%s:branches:%s", cred.Name, params.Project)
	}
	targetPackage := params.Bundle

	apiURL, err := url.Parse(fmt.Sprintf("%s/source/%s/%s", cred.GetAPiAddr(), params.Project, params.Bundle))
	if err != nil {
		return nil, BranchResult{}, fmt.Errorf("failed to parse API URL: %w", err)
	}
	q := apiURL.Query()
	if params.Copy {
		q.Set("cmd", "copy")
	} else {
		q.Set("cmd", "branch")
	}
	q.Set("target_project", targetProject)
	q.Set("target_package", targetPackage)
	apiURL.RawQuery = q.Encode()

	httpReq, err := http.NewRequestWithContext(ctx, "POST", apiURL.String(), nil)
	if err != nil {
		return nil, BranchResult{}, fmt.Errorf("failed to create request: %w", err)
	}

	httpReq.Header.Set("User-Agent", "osc-mcp")
	httpReq.SetBasicAuth(cred.Name, cred.Passwd)

	client := &http.Client{}
	resp, err := client.Do(httpReq)
	if err != nil {
		return nil, BranchResult{}, fmt.Errorf("failed to execute request: %w", err)
	}
	defer resp.Body.Close()

	branchAlreadyExists := false
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		bodyStr := string(body)
		if resp.StatusCode == http.StatusBadRequest &&
			(strings.Contains(bodyStr, "branch target package already exists") ||
				strings.Contains(bodyStr, "already exists")) {
			slog.Info("Branch already exists, will checkout instead",
				"project", targetProject, "package", targetPackage)
			branchAlreadyExists = true
		} else {
			return nil, BranchResult{}, fmt.Errorf("api request failed with status %s: %s", resp.Status, bodyStr)
		}
	}

	ClearOscCookieCache()

	checkoutDir := filepath.Join(cred.TempDir, targetProject, targetPackage)
	if _, err := os.Stat(checkoutDir); err == nil {
		if branchAlreadyExists {
			repairCmd := exec.CommandContext(ctx, "osc", "repairwc", ".")
			repairCmd.Dir = checkoutDir
			repairOutput, repairErr := repairCmd.CombinedOutput()
			if repairErr != nil {
				slog.Warn("repairwc failed, removing and re-checking out",
					"error", repairErr, "output", string(repairOutput))
				os.RemoveAll(checkoutDir)
				cmd := exec.CommandContext(ctx, "osc", "checkout", targetProject, targetPackage)
				cmd.Dir = cred.TempDir
				output, err := cmd.CombinedOutput()
				if err != nil {
					return nil, BranchResult{}, fmt.Errorf("failed to run '%s': %w\n%s", cmd.String(), err, string(output))
				}
			} else {
				cmd := exec.CommandContext(ctx, "osc", "update")
				cmd.Dir = checkoutDir
				output, err := cmd.CombinedOutput()
				if err != nil {
					slog.Warn("osc update failed, removing and re-checking out",
						"error", err, "output", string(output))
					os.RemoveAll(checkoutDir)
					cmd2 := exec.CommandContext(ctx, "osc", "checkout", targetProject, targetPackage)
					cmd2.Dir = cred.TempDir
					output2, err2 := cmd2.CombinedOutput()
					if err2 != nil {
						return nil, BranchResult{}, fmt.Errorf("failed to run '%s': %w\n%s", cmd2.String(), err2, string(output2))
					}
				}
			}
		} else {
			cmd := exec.CommandContext(ctx, "osc", "update")
			cmd.Dir = checkoutDir
			output, err := cmd.CombinedOutput()
			if err != nil {
				return nil, BranchResult{}, fmt.Errorf("failed to run '%s' in '%s': %w\n%s", cmd.String(), checkoutDir, err, string(output))
			}
		}
	} else if os.IsNotExist(err) {
		parentDir := filepath.Join(cred.TempDir, targetProject)
		os.MkdirAll(parentDir, 0755)
		cmd := exec.CommandContext(ctx, "osc", "checkout", targetProject, targetPackage)
		cmd.Dir = cred.TempDir
		output, err := cmd.CombinedOutput()
		if err != nil {
			return nil, BranchResult{}, fmt.Errorf("failed to run '%s': %w\n%s", cmd.String(), err, string(output))
		}
	} else {
		return nil, BranchResult{}, fmt.Errorf("failed to check directory '%s': %w", checkoutDir, err)
	}

	result := BranchResult{
		TargetProject: targetProject,
		TargetPackage: targetPackage,
		CheckoutDir:   checkoutDir,
	}

	return nil, result, nil
}
