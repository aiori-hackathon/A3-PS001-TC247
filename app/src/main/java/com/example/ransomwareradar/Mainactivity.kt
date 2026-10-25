package com.example.ransomwareradar

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.FileObserver
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.ransomwareradar.ui.theme.RansomwareRadarTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentLinkedDeque
import java.util.concurrent.atomic.AtomicBoolean

private const val WINDOW_MS = 5_000L
private const val COOLDOWN_MS = 10_000L
private const val MIN_EVENTS = 12
private const val MIN_MODIFIES = 6
private const val MIN_RENAMES = 2
private const val MIN_DELETES = 6
private const val HIGH_DELETES = 8
private const val HIGH_EVENTS = 20

data class LabEvent(
    val time: Long,
    val type: String,
    val detail: String
)

private class PersistentLogger(context: Context) {
    private val logDir = File(context.filesDir, "ransomware_radar_logs").apply {
        if (!exists()) mkdirs()
    }
    private val eventFile = File(logDir, "events.jsonl")
    private val alertFile = File(logDir, "alerts.jsonl")
    private val healthFile = File(logDir, "monitor_health.log")
    private val lock = Any()

    fun event(event: LabEvent) = appendJson(eventFile, JSONObject().apply {
        put("timestamp_ms", event.time)
        put("timestamp", timestamp(event.time))
        put("type", event.type)
        put("detail", event.detail)
    })

    fun alert(time: Long, severity: String, reason: String, counts: Map<String, Int>) =
        appendJson(alertFile, JSONObject().apply {
            put("timestamp_ms", time)
            put("timestamp", timestamp(time))
            put("severity", severity)
            put("reason", reason)
            put("counts", JSONObject(counts))
        })

    fun health(message: String) {
        synchronized(lock) {
            healthFile.appendText("${timestamp(System.currentTimeMillis())} | $message\n")
        }
    }

    fun exportText(): String = buildString {
        appendLine("=== EVENTS (JSON Lines) ===")
        if (eventFile.exists()) appendLine(eventFile.readText())
        appendLine("\n=== ALERTS (JSON Lines) ===")
        if (alertFile.exists()) appendLine(alertFile.readText())
        appendLine("\n=== MONITOR HEALTH ===")
        if (healthFile.exists()) appendLine(healthFile.readText())
    }

    private fun appendJson(file: File, json: JSONObject) {
        synchronized(lock) { file.appendText(json.toString() + "\n") }
    }

    private fun timestamp(time: Long): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US).format(Date(time))
}

private class LabMonitor(
    private val context: Context,
    private val logger: PersistentLogger,
    private val onEvent: (LabEvent) -> Unit,
    private val onAlert: (String) -> Unit
) {
    private val running = AtomicBoolean(false)
    private val recentEvents = ConcurrentLinkedDeque<LabEvent>()
    @Volatile private var observer: FileObserver? = null
    @Volatile private var lastAlertAt = 0L
    @Volatile private var lastHealthCheck = 0L
    @Volatile private var lastSeverityRank = 0
    @Volatile private var lastReasonKeys: Set<String> = emptySet()

    val labFolder: File
        get() = File(context.filesDir, "ransomware_radar_lab")

    fun start(): Boolean {
        if (!running.compareAndSet(false, true)) return false

        recentEvents.clear()
        lastAlertAt = 0L
        lastSeverityRank = 0
        lastReasonKeys = emptySet()

        if (!labFolder.exists() && !labFolder.mkdirs()) {
            running.set(false)
            logger.health("ERROR: Could not create lab directory")
            onAlert("Could not create the lab directory.")
            return false
        }

        return try {
            val mask = FileObserver.CREATE or FileObserver.MODIFY or
                    FileObserver.DELETE or FileObserver.MOVED_FROM or
                    FileObserver.MOVED_TO or FileObserver.CLOSE_WRITE or
                    FileObserver.ATTRIB or FileObserver.DELETE_SELF or
                    FileObserver.MOVE_SELF

            observer = object : FileObserver(labFolder.absolutePath, mask) {
                override fun onEvent(event: Int, path: String?) {
                    if (!running.get()) return
                    val flags = event and FileObserver.ALL_EVENTS
                    val type = when {
                        flags and FileObserver.CREATE != 0 -> "CREATE"
                        flags and FileObserver.MODIFY != 0 -> "MODIFY"
                        flags and FileObserver.DELETE != 0 -> "DELETE"
                        flags and FileObserver.MOVED_FROM != 0 -> "MOVED_FROM"
                        flags and FileObserver.MOVED_TO != 0 -> "MOVED_TO"
                        flags and FileObserver.CLOSE_WRITE != 0 -> "CLOSE_WRITE"
                        flags and FileObserver.ATTRIB != 0 -> "ATTRIB"
                        flags and FileObserver.DELETE_SELF != 0 -> "DELETE_SELF"
                        flags and FileObserver.MOVE_SELF != 0 -> "MOVE_SELF"
                        else -> "OTHER"
                    }
                    val eventRecord = LabEvent(
                        System.currentTimeMillis(),
                        type,
                        path ?: "(path not supplied by Android)"
                    )
                    recentEvents.addLast(eventRecord)
                    logger.event(eventRecord)
                    onEvent(eventRecord)
                    evaluateWindow(eventRecord.time)
                }
            }
            observer?.startWatching()
            logger.health("Monitoring started: ${labFolder.absolutePath}")
            true
        } catch (e: Exception) {
            running.set(false)
            logger.health("ERROR: Monitoring start failed: ${e.message}")
            observer?.stopWatching()
            observer = null
            onAlert("Monitoring failed to start: ${e.message ?: "unknown error"}")
            false
        }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        observer?.stopWatching()
        observer = null
        logger.health("Monitoring stopped by user")
    }

    fun healthCheck() {
        if (!running.get()) return
        val now = System.currentTimeMillis()
        if (now - lastHealthCheck >= 10_000L) {
            lastHealthCheck = now
            logger.health("Health check: watcher marked active")
        }
    }

    private fun evaluateWindow(now: Long) {
        // Drop events older than the sliding window.
        while (true) {
            val first = recentEvents.peekFirst() ?: break
            if (now - first.time <= WINDOW_MS) break
            recentEvents.pollFirst()
        }

        val snapshot = recentEvents.toList()
        val creates = snapshot.count { it.type == "CREATE" }
        val modifies = snapshot
            .filter { it.type == "MODIFY" || it.type == "CLOSE_WRITE" }
            .map { it.detail }
            .distinct()
            .size
        val deletes = snapshot.count { it.type == "DELETE" }
        val movedFrom = snapshot.count { it.type == "MOVED_FROM" }
        val movedTo = snapshot.count { it.type == "MOVED_TO" }
        val moveEvents = movedFrom + movedTo
        // Count only matched source and destination events.
        val renameLikePairs = minOf(movedFrom, movedTo)

        val reasons = mutableListOf<String>()
        val keys = mutableSetOf<String>()
        

        if (snapshot.size >= MIN_EVENTS || modifies >= MIN_MODIFIES) {
            reasons.add("high activity: ${snapshot.size} events, $modifies files modified")
            keys.add("volume")
        }
        if (deletes >= MIN_DELETES) {
            reasons.add("delete burst ($deletes deletes)")
            keys.add("delete")
        }
        if (renameLikePairs >= MIN_RENAMES) {
            reasons.add("rename-like activity ($renameLikePairs move pairs)")
            keys.add("rename")
        }

        if (reasons.isEmpty()) return

        val severity =
            if (deletes >= HIGH_DELETES || snapshot.size >= HIGH_EVENTS) "HIGH" else "SUSPICIOUS"
        val rank = if (severity == "HIGH") 2 else 1

        val inCooldown = now - lastAlertAt < COOLDOWN_MS
        val escalated = rank > lastSeverityRank
        val hasNewReason = !lastReasonKeys.containsAll(keys)

        // Cooldown only blocks alerts that are neither more severe nor about something new.
        if (inCooldown && !escalated && !hasNewReason) return

        if (inCooldown) {
            lastSeverityRank = maxOf(lastSeverityRank, rank)
            lastReasonKeys = lastReasonKeys + keys
        } else {
            lastAlertAt = now
            lastSeverityRank = rank
            lastReasonKeys = keys
        }

        val reason = reasons.joinToString("; ")
        val counts = mapOf(
            "events" to snapshot.size,
            "creates" to creates,
            "modifies_or_close_writes" to modifies,
            "deletes" to deletes,
            "moved_from" to movedFrom,
            "moved_to" to movedTo,
            "move_events" to moveEvents,
            "estimated_move_pairs" to renameLikePairs
        )

        logger.alert(now, severity, reason, counts)
        logger.health("ALERT: $severity | $reason")
        onAlert("$severity: $reason. Counts: $counts")
    }
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            RansomwareRadarTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    RadarScreen()
                }
            }
        }
    }
}

@Composable
fun RadarScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val logger = remember { PersistentLogger(context.applicationContext) }
    val events = remember { mutableStateListOf<LabEvent>() }
    var status by remember { mutableStateOf("Ready. Monitoring is stopped.") }
    var result by remember { mutableStateOf("No simulation run yet.") }
    var simulationRunning by remember { mutableStateOf(false) }
    var monitor by remember { mutableStateOf<LabMonitor?>(null) }
    var monitorActive by remember { mutableStateOf(false) }
    var latestAlert by remember { mutableStateOf("No alerts in this session.") }
    var healthJob by remember { mutableStateOf<Job?>(null) }

    fun addEvent(event: LabEvent) {
        scope.launch {
            events.add(event)
            if (events.size > 300) events.removeAt(0)
        }
    }

    fun startMonitoring() {
        if (monitorActive) return
        val newMonitor = LabMonitor(
            context.applicationContext,
            logger,
            onEvent = { addEvent(it) },
            onAlert = { message -> scope.launch { latestAlert = message } }
        )
        monitor = newMonitor
        latestAlert = "No alerts in this monitoring session."
        monitorActive = newMonitor.start()
        status = if (monitorActive) {
            "Monitoring active: ${newMonitor.labFolder.name}"
        } else {
            "Monitoring could not start. Check health logs."
        }
        if (monitorActive) {
            healthJob?.cancel()
            healthJob = scope.launch {
                while (isActive && monitorActive) {
                    delay(10_000L)
                    newMonitor.healthCheck()
                }
            }
        }
    }

    fun stopMonitoring() {
        monitor?.stop()
        monitor = null
        monitorActive = false
        healthJob?.cancel()
        healthJob = null
        status = "Monitoring stopped."
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text(
            "Ransomware Radar",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold
        )
        Text("Controlled Android activity early-warning demo")

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp)) {
                Text("Lab status", fontWeight = FontWeight.Bold)
                Text(status)
                Spacer(Modifier.height(8.dp))
                Text(
                    "Only the app-private ransomware_radar_lab folder is used. " +
                            "This is not a system-wide monitor. Use only disposable test files."
                )
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { startMonitoring() },
                enabled = !monitorActive,
                modifier = Modifier.weight(1f)
            ) { Text("Start Monitor") }

            OutlinedButton(
                onClick = { stopMonitoring() },
                enabled = monitorActive,
                modifier = Modifier.weight(1f)
            ) { Text("Stop Monitor") }
        }

        Button(
            onClick = {
                if (simulationRunning) return@Button
                simulationRunning = true
                result = "Creating safe dummy-file activity..."
                scope.launch {
                    try {
                        val generated = withContext(Dispatchers.IO) {
                            simulateSafeActivity(context.applicationContext)
                        }
                        generated.forEach { event ->
                            logger.event(LabEvent(event.time, "SIM_${event.type}", event.detail))
                            addEvent(event)
                        }
                        result = "Simulation completed. Generated ${generated.size} actions. " +
                                "The live monitor may also record OS-reported filesystem events."
                    } catch (e: Exception) {
                        result = "Simulation failed: ${e.message ?: "unknown error"}"
                        logger.health("ERROR: Simulation failed: ${e.message}")
                    } finally {
                        simulationRunning = false
                    }
                }
            },
            enabled = !simulationRunning,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (simulationRunning) "Simulation running..." else "Run Safe Simulation")
        }

        OutlinedButton(
            onClick = {
                scope.launch {
                    val report = withContext(Dispatchers.IO) { logger.exportText() }
                    val sendIntent = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_SUBJECT, "Ransomware Radar monitoring logs")
                        putExtra(Intent.EXTRA_TEXT, report)
                    }
                    try {
                        context.startActivity(Intent.createChooser(sendIntent, "Export monitoring logs"))
                    } catch (e: Exception) {
                        latestAlert = "Could not open share sheet: ${e.message}"
                    }
                }
            },
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("Export Logs")
        }

        OutlinedButton(
            onClick = {
                scope.launch {
                    try {
                        result = "Running normal activity test..."
                        val event = withContext(Dispatchers.IO) {
                            simulateNormalActivity(context.applicationContext)
                        }
                        logger.event(event)
                        addEvent(event)
                        result = "Normal activity test completed."
                    } catch (e: Exception) {
                        result = "Normal test failed: ${e.message}"
                    }
                }
            },
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("Run Normal Activity Test")
        }

        Text("Latest detection result", fontWeight = FontWeight.Bold)
        Text(latestAlert)
        Text("Simulation status", fontWeight = FontWeight.Bold)
        Text(result)

        Text("Activity log (latest first)", fontWeight = FontWeight.Bold)
        if (events.isEmpty()) {
            Text("No events recorded in this screen session.")
        } else {
            events.asReversed().forEach { event ->
                Text("${event.type}: ${event.detail}")
            }
        }
    }
}

private fun simulateNormalActivity(context: Context): LabEvent {
    val labFolder = File(context.filesDir, "ransomware_radar_lab")
    if (!labFolder.exists() && !labFolder.mkdirs()) {
        error("Could not create the lab folder.")
    }

    val file = File(labFolder, "normal_test_${System.currentTimeMillis()}.txt")
    file.writeText("Harmless normal activity test.\n")
    file.appendText("One ordinary modification.\n")

    return LabEvent(System.currentTimeMillis(), "NORMAL_TEST", file.name)
}

private fun simulateSafeActivity(context: Context): List<LabEvent> {
    val labFolder = File(context.filesDir, "ransomware_radar_lab")
    if (!labFolder.exists() && !labFolder.mkdirs()) {
        error("Could not create the lab folder.")
    }

    val generated = mutableListOf<LabEvent>()
    fun record(type: String, detail: String) {
        generated.add(LabEvent(System.currentTimeMillis(), type, detail))
    }

    // Files stay directly in the watched directory: FileObserver does not
    // watch child directories. Unique names preserve previous runs.
    val runPrefix = "run_${System.currentTimeMillis()}"

    for (i in 1..8) {
        val file = File(labFolder, "${runPrefix}_dummy_$i.txt")
        file.writeText("Harmless safe test content $i\n")
        record("CREATE", file.name)
    }
    for (i in 1..8) {
        val file = File(labFolder, "${runPrefix}_dummy_$i.txt")
        file.appendText("Safe simulated test modification\n")
        record("MODIFY", file.name)
    }
    for (i in 1..3) {
        val original = File(labFolder, "${runPrefix}_dummy_$i.txt")
        val renamed = File(labFolder, "${runPrefix}_renamed_$i.txt")
        if (original.renameTo(renamed)) {
            record("RENAME", "${original.name} -> ${renamed.name}")
        } else {
            error("Could not rename ${original.name}")
        }
    }
    return generated
}