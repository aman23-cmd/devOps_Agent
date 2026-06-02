document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const metricTotalFixes = document.getElementById('metric-total-fixes');
    const metricSuccessRate = document.getElementById('metric-success-rate');
    const metricAvgDuration = document.getElementById('metric-avg-duration');
    const metricAutoApplied = document.getElementById('metric-auto-applied');
    const tableBody = document.getElementById('table-body');
    const lastUpdated = document.getElementById('last-updated-time');
    const refreshBtn = document.getElementById('refresh-btn');

    // Base API URL (assuming the frontend is served by the FastAPI app on the same origin)
    const API_BASE = window.location.origin;

    // Fetch Dashboard Data
    async function fetchDashboardData() {
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
            // Don't show an aggressive error, just let it retry silently
        }
    }

    function updateMetrics(analytics) {
        if (!analytics || analytics.error) return;

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
            tableBody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No pipeline failures recorded yet.</td></tr>`;
            return;
        }

        tableBody.innerHTML = ''; // Clear loading row

        records.forEach(record => {
            const tr = document.createElement('tr');
            
            // Format time
            const date = new Date(record.created_at);
            const timeString = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            
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
                <td>${record.action_taken ? record.action_taken.substring(0, 30) + '...' : 'Analyzing...'}</td>
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

    // Event Listeners
    refreshBtn.addEventListener('click', () => {
        const icon = refreshBtn.querySelector('i');
        icon.classList.add('fa-spin');
        fetchDashboardData().finally(() => {
            setTimeout(() => icon.classList.remove('fa-spin'), 500);
        });
    });

    // Initial Fetch
    fetchDashboardData();

    // Auto-refresh every 10 seconds
    setInterval(fetchDashboardData, 10000);
});
