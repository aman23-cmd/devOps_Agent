document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const metricTotalFixes = document.getElementById('metric-total-fixes');
    const metricSuccessRate = document.getElementById('metric-success-rate');
    const metricAvgDuration = document.getElementById('metric-avg-duration');
    const metricAutoApplied = document.getElementById('metric-auto-applied');
    const tableBody = document.getElementById('table-body');
    const lastUpdated = document.getElementById('last-updated-time');
    const refreshBtn = document.getElementById('refresh-btn');
    const demoBtn = document.getElementById('demo-btn');

    let isDemoMode = false;
    let demoRecords = [];
    let demoMetrics = {
        total_fixes: 0,
        successful_fixes: 0,
        auto_applied_fixes: 0,
        avg_duration_seconds: 0
    };

    // Base API URL (assuming the frontend is served by the FastAPI app on the same origin)
    const API_BASE = window.location.origin;

    // Fetch Dashboard Data
    async function fetchDashboardData() {
        if (isDemoMode) return; // Skip real fetching if in demo mode
        
        try {
            updateLastUpdated();
            
            // 1. Fetch Analytics Summary
            const statusRes = await fetch(`${API_BASE}/status`);
            if (statusRes.ok) {
                const data = await statusRes.json();
                updateMetrics(data.analytics);
            }

            // 2. Fetch Recent Interventions
            const recentRes = await fetch(`${API_BASE}/status/recent?limit=15`);
            if (recentRes.ok) {
                const data = await recentRes.json();
                updateTable(data.records);
            }
        } catch (error) {
            console.error('Error fetching dashboard data:', error);
        }
    }

    function updateMetrics(analytics) {
        if (!analytics || analytics.error) {
            if (!isDemoMode) {
                metricTotalFixes.textContent = "-";
                metricSuccessRate.textContent = "-";
                metricAutoApplied.textContent = "-";
                metricAvgDuration.textContent = "-";
            }
            return;
        }

        const total = analytics.total_fixes || 0;
        const success = analytics.successful_fixes || 0;
        const auto = analytics.auto_applied_fixes || 0;
        const duration = analytics.avg_duration_seconds || 0;

        // Calculate Success Rate
        const rate = total > 0 ? Math.round((success / total) * 100) : 0;

        metricTotalFixes.textContent = total;
        metricSuccessRate.textContent = `${rate}%`;
        metricAutoApplied.textContent = auto;
        
        // Format duration (e.g., 45.2s)
        metricAvgDuration.textContent = duration > 0 ? `${duration.toFixed(1)}s` : '0s';
    }

    function updateTable(records) {
        if (!records || records.length === 0) {
            tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No pipeline failures recorded yet. (Click 'Run Demo Simulation' to see it in action!)</td></tr>`;
            return;
        }

        tableBody.innerHTML = ''; // Clear loading row

        records.forEach(record => {
            const tr = document.createElement('tr');
            
            // Format time
            const date = new Date(record.created_at);
            const timeString = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            
            // Format status
            let statusClass = 'status-pending';
            let statusText = 'Pending';
            
            if (record.fix_status === 'success' || record.fix_status === 'auto_applied') {
                statusClass = 'status-success';
                statusText = 'Success';
            } else if (record.fix_status === 'failed') {
                statusClass = 'status-failure';
                statusText = 'Failed';
            } else if (record.fix_status === 'requires_review') {
                statusClass = 'status-pending';
                statusText = 'Review Needed';
            }

            // Format confidence bar
            const confidence = record.confidence_score || 0;
            let confidenceColor = 'var(--accent-blue)';
            if (confidence < 0.5) confidenceColor = 'var(--warning)';
            if (confidence > 0.8) confidenceColor = 'var(--success)';

            // Format Badge Category
            const category = record.category || 'unknown';
            let badgeClass = 'default';
            if (category.includes('regression')) badgeClass = 'code_regression';
            else if (category.includes('dependency')) badgeClass = 'dependency_issue';
            else if (category.includes('flaky')) badgeClass = 'flaky_test';
            else if (category.includes('infrastructure') || category.includes('config')) badgeClass = 'infrastructure';

            tr.innerHTML = `
                <td class="repo-name">
                    <i class="fa-brands fa-github" style="margin-right: 8px; color: var(--text-muted);"></i> 
                    ${record.repo_name || 'unknown/repo'}
                </td>
                <td>
                    <span class="badge ${badgeClass}">
                        ${category.replace('_', ' ')}
                    </span>
                </td>
                <td>
                    <div>${Math.round(confidence * 100)}%</div>
                    <div class="confidence-bar">
                        <div class="confidence-fill" style="width: ${confidence * 100}%; background: ${confidenceColor}"></div>
                    </div>
                </td>
                <td>${record.action_taken ? record.action_taken.substring(0, 35) + '...' : 'Agent analyzing codebase...'}</td>
                <td>${record.fix_duration_seconds ? record.fix_duration_seconds.toFixed(1) + 's' : '-'}</td>
                <td>
                    <span class="status-indicator-dot ${statusClass}">${statusText}</span>
                </td>
                <td style="color: var(--text-muted); font-size: 13px;">${timeString}</td>
            `;
            tableBody.appendChild(tr);
        });
    }

    function updateLastUpdated() {
        const now = new Date();
        lastUpdated.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    // --- DEMO SIMULATION LOGIC ---
    function runDemoSimulation() {
        isDemoMode = true;
        demoBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Simulating...';
        demoBtn.disabled = true;

        // Reset Graph
        document.getElementById('workflow-section').style.display = 'block';
        const statusText = document.getElementById('workflow-status-text');
        statusText.textContent = 'Event received, starting pipeline...';
        statusText.style.color = 'var(--text-primary)';
        
        const nodes = ['node-webhook', 'node-agent', 'node-fix', 'node-git', 'node-slack'];
        const edges = ['edge-1', 'edge-2', 'edge-3a', 'edge-3b'];
        
        nodes.forEach(n => document.getElementById(n).className = 'wf-node ' + (n.includes('split') ? (n.includes('top') ? 'wf-split-top' : 'wf-split-bottom') : ''));
        edges.forEach(e => document.getElementById(e).className = 'wf-edge ' + (e.includes('3a') ? 'split-edge-top' : (e.includes('3b') ? 'split-edge-bottom' : '')));

        const newRecord = {
            id: Math.random().toString(36).substring(7),
            repo_name: 'aman23-cmd/devOps_Agent',
            category: 'code_regression',
            confidence_score: 0.2, // Starts low, agent is thinking
            action_taken: 'Reading pytest logs...',
            fix_status: 'requires_review', // Pending
            fix_duration_seconds: null,
            created_at: new Date().toISOString()
        };

        // Add to front of demo list
        demoRecords.unshift(newRecord);
        updateTable(demoRecords);
        updateLastUpdated();

        // ── Node 1: Webhook ──
        document.getElementById('node-webhook').classList.add('active');
        
        setTimeout(() => {
            document.getElementById('node-webhook').classList.replace('active', 'completed');
            document.getElementById('edge-1').classList.add('active');
            document.getElementById('node-agent').classList.add('active');
            statusText.textContent = 'Agent diagnosing root cause...';
        }, 1000);

        setTimeout(() => {
            document.getElementById('node-agent').classList.replace('active', 'completed');
            document.getElementById('edge-2').classList.add('active');
            document.getElementById('node-fix').classList.add('active');
            statusText.textContent = 'Generating code patch...';
            
            // Update table mid-flight
            demoRecords[0].confidence_score = 0.6;
            demoRecords[0].action_taken = 'Drafting fix...';
            updateTable(demoRecords);
        }, 2500);

        setTimeout(() => {
            document.getElementById('node-fix').classList.replace('active', 'completed');
            document.getElementById('edge-3a').classList.add('active');
            document.getElementById('edge-3b').classList.add('active');
            document.getElementById('node-git').classList.add('active');
            document.getElementById('node-slack').classList.add('active');
            statusText.textContent = 'Committing code and notifying Slack...';
        }, 4000);

        // Final Resolution
        setTimeout(() => {
            document.getElementById('node-git').classList.replace('active', 'completed');
            document.getElementById('node-slack').classList.replace('active', 'completed');
            
            statusText.textContent = 'Pipeline fixed successfully!';
            statusText.style.color = 'var(--success)';

            newRecord.confidence_score = 0.92;
            newRecord.action_taken = 'Auto-applied patch for syntax error in worker.py';
            newRecord.fix_status = 'auto_applied';
            newRecord.fix_duration_seconds = 4.2;
            
            // Update metrics
            demoMetrics.total_fixes += 1;
            demoMetrics.successful_fixes += 1;
            demoMetrics.auto_applied_fixes += 1;
            demoMetrics.avg_duration_seconds = 4.2;

            updateTable(demoRecords);
            updateMetrics(demoMetrics);
            updateLastUpdated();
            
            demoBtn.innerHTML = '<i class="fa-solid fa-play"></i> Run Demo Simulation';
            demoBtn.disabled = false;
            
            // Stop edge flow animation after a while
            setTimeout(() => {
                edges.forEach(e => document.getElementById(e).classList.remove('active'));
            }, 2000);
        }, 5500);
    }

    // Event Listeners
    refreshBtn.addEventListener('click', () => {
        isDemoMode = false; // Turn off demo mode
        const icon = refreshBtn.querySelector('i');
        icon.classList.add('fa-spin');
        fetchDashboardData().finally(() => {
            setTimeout(() => icon.classList.remove('fa-spin'), 500);
        });
    });

    if (demoBtn) {
        demoBtn.addEventListener('click', runDemoSimulation);
    }

    // Initial Fetch
    fetchDashboardData();

    // Auto-refresh every 10 seconds
    setInterval(() => {
        if (!isDemoMode) fetchDashboardData();
    }, 10000);
});
