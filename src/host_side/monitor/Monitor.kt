import android.os.FileObserver
import android.os.Process as AndroidProcess
import java.io.BufferedWriter
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.time.Instant
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.ln
import kotlin.system.exitProcess

// ---------------------------------------------------------------------------
// Configuration & CLI arguments (Section 6.1)
// ---------------------------------------------------------------------------
data class Config(
    val root: String = "/data/media/0",
    val windowMs: Long = 5000L,
    val minFiles: Int = 6,
    val minScore: Double = 15.0,
    val kill: Boolean = true,
    val registryDir: String = "/data/local/tmp/radar_registry",
    val outDir: String = "/data/local/tmp/radar_logs",
    val baselineJson: String = "",
    val cooldownMs: Long = 3000L,
    val selfTest: Boolean = false,
    val checkEnv: Boolean = false,
    // --check-dirs: verify every watched dir sees writes made through the /sdcard (FUSE) path
    val checkDirs: Boolean = false,
    val probeRoot: String = "/sdcard",
    val expectDirs: List<String> = listOf(
        "DCIM/Camera", "Pictures/Screenshots", "Android/media", "Documents/College",
        "Documents/Personal", "Download", "Music", "Movies", "Backups"
    )
)

private fun parseArgs(args: Array<String>): Config {
    var cfg = Config()
    var i = 0
    while (i < args.size) {
        when (args[i]) {
            "--root" -> cfg = cfg.copy(root = args.getOrNull(++i) ?: cfg.root)
            "--window-ms" -> cfg = cfg.copy(windowMs = args.getOrNull(++i)?.toLongOrNull() ?: cfg.windowMs)
            "--min-files" -> cfg = cfg.copy(minFiles = args.getOrNull(++i)?.toIntOrNull() ?: cfg.minFiles)
            "--min-score" -> cfg = cfg.copy(minScore = args.getOrNull(++i)?.toDoubleOrNull() ?: cfg.minScore)
            "--kill" -> cfg = cfg.copy(kill = args.getOrNull(++i)?.lowercase() != "false")
            "--registry" -> cfg = cfg.copy(registryDir = args.getOrNull(++i) ?: cfg.registryDir)
            "--out" -> cfg = cfg.copy(outDir = args.getOrNull(++i) ?: cfg.outDir)
            "--baseline-json" -> cfg = cfg.copy(baselineJson = args.getOrNull(++i) ?: "")
            "--cooldown-ms" -> cfg = cfg.copy(cooldownMs = args.getOrNull(++i)?.toLongOrNull() ?: cfg.cooldownMs)
            "--selftest" -> cfg = cfg.copy(selfTest = true)
            "--check-env" -> cfg = cfg.copy(checkEnv = true)
            "--check-dirs" -> cfg = cfg.copy(checkDirs = true)
            "--probe-root" -> cfg = cfg.copy(probeRoot = (args.getOrNull(++i) ?: cfg.probeRoot).trimEnd('/'))
            "--expect" -> cfg = cfg.copy(
                expectDirs = (args.getOrNull(++i) ?: "").split(',').map { it.trim().trim('/') }.filter { it.isNotEmpty() }
            )
        }
        i++
    }
    return cfg.copy(root = cfg.root.trimEnd('/').ifEmpty { "/" })
}

// ---------------------------------------------------------------------------
// High-resolution device clock & JSON helpers (Section 6.7)
// ---------------------------------------------------------------------------
private fun nowNs(): Long {
    val now = Instant.now()
    return now.epochSecond * 1_000_000_000L + now.nano.toLong()
}

private fun jsonEscape(s: String): String {
    val sb = StringBuilder(s.length + 8)
    for (ch in s) {
        when (ch) {
            '\\' -> sb.append("\\\\")
            '"' -> sb.append("\\\"")
            '\n' -> sb.append("\\n")
            '\r' -> sb.append("\\r")
            '\t' -> sb.append("\\t")
            else -> sb.append(ch)
        }
    }
    return sb.toString()
}

// ---------------------------------------------------------------------------
// Data structures for raw events, normalized detector actions, and /proc stats
// ---------------------------------------------------------------------------
data class RawFsEvent(
    val tsNs: Long,
    val mask: Int,
    val dirPath: String,
    val relName: String
)

enum class ActionKind {
    BASELINE_WRITE,
    BASELINE_RENAME,
    BASELINE_DELETE,
    EXT_CHANGE,
    NEW_FILE_CREATE
}

data class ScoredAction(
    val tsNs: Long,
    val kind: ActionKind,
    val weight: Double,
    val baselineKey: String?, // non-null if this action touched a baseline file
    val path: String,
    val reason: String
)

data class CandidateProc(
    val name: String,
    val pid: Int,
    val cmdline: String,
    val openWatchedFds: Int,
    val wcharDelta: Long,
    val syscwDelta: Long,
    val totalWchar: Long,
    val totalSyscw: Long,
    val activeChildren: List<Int>
) {
    // Score candidate activity so if both benign and attack are registered,
    // or if the attacker is mid-sleep (between 50ms/500ms loop iterations),
    // we still attribute accurately.
    fun activityScore(): Long {
        var s = 0L
        if (openWatchedFds > 0) s += 1_000_000L * openWatchedFds
        if (wcharDelta > 0) s += 10_000L + wcharDelta
        if (syscwDelta > 0) s += 5_000L * syscwDelta
        if (activeChildren.isNotEmpty()) s += 50_000L * activeChildren.size
        if (totalWchar > 0) s += (totalWchar.coerceAtMost(100_000_000L) / 1024L).coerceAtLeast(100L)
        if (totalSyscw > 0) s += totalSyscw.coerceAtMost(10_000L)
        return s
    }
}

// ---------------------------------------------------------------------------
// Core Monitor Engine (Sections 6.2 - 6.7)
// ---------------------------------------------------------------------------
class RadarMonitor(private val cfg: Config) {

    private val running = AtomicBoolean(true)
    private val rawQueue = LinkedBlockingQueue<RawFsEvent>(65536)

    // stdout is streamed through a bounded queue + pump thread so that a stalled reader
    // (adb pipe nobody drains) can never block event processing, alerting or the kill.
    private val stdoutQueue = LinkedBlockingQueue<String>(8192)
    @Volatile private var stdoutDropped = 0L
    private val stdoutPump = Thread {
        try {
            while (true) {
                println(stdoutQueue.take())
                System.out.flush()
            }
        } catch (_: Throwable) {}
    }.apply { isDaemon = true; name = "stdout-pump"; start() }

    @Volatile var ready = false
    val probeSeen = ConcurrentHashMap<String, Boolean>()
    fun watchedDirs(): List<String> = observers.keys.sorted()
    fun relOf(absPath: String): String = toRelPath(absPath)

    // Strong references to FileObservers so GC never collects them (Section 6.2)
    private val observers = ConcurrentHashMap<String, FileObserver>()

    // Baseline file set: relative paths from cfg.root -> original file size (Section 6.3)
    private val baselineFiles = ConcurrentHashMap<String, Long>()
    // Tracks renamed baseline files (newRelPath -> originalBaselineRelPath) so
    // post-rename writes/deletes still map to the same distinct baseline file
    private val renamedBaselineMap = ConcurrentHashMap<String, String>()
    private val knownExtensions = HashSet<String>()

    // Event normalisation state (Section 6.4)
    private val lastMovedToNs = HashMap<String, Long>()
    private val lastWriteCollapsedNs = HashMap<String, Long>()
    private var pendingMovedFromPath: String? = null
    private var pendingMovedFromBaseKey: String? = null
    private var pendingMovedFromTsNs: Long = 0L

    // Sliding window state (Section 6.5)
    private val windowActions = ArrayDeque<ScoredAction>()
    private var lastAlertTsNs: Long = 0L

    // Log writers (Section 6.7)
    private lateinit var eventsWriter: BufferedWriter
    private lateinit var alertsWriter: BufferedWriter
    private lateinit var killWriter: BufferedWriter
    private lateinit var healthWriter: BufferedWriter

    companion object {
        private const val IN_ISDIR = 0x40000000
        const val PROBE_PREFIX = ".radar_probe_"
        private const val POST_MOVE_SUPPRESS_NS = 50_000_000L   // 50 ms
        private const val WRITE_COLLAPSE_NS = 200_000_000L      // 200 ms
        private const val RENAME_PAIR_NS = 250_000_000L         // 250 ms

        private val WATCH_MASK =
            FileObserver.CREATE or
            FileObserver.MODIFY or
            FileObserver.CLOSE_WRITE or
            FileObserver.DELETE or
            FileObserver.MOVED_FROM or
            FileObserver.MOVED_TO

        private val BENIGN_TEMP_SUFFIXES = listOf(
            ".tmp", "-journal", "-wal", "-shm", ".pending", ".bak"
        )

        private val SUSPICIOUS_EXTENSIONS = setOf(
            "locked", "enc", "encrypted", "crypt", "crypted", "ransom", "locked1"
        )

        private val RANSOM_NOTE_KEYWORDS = listOf(
            "readme_restore", "how_to_decrypt", "decrypt_files", "ransom", "restore_files"
        )
    }

    fun start() {
        initLoggers()
        val rootDir = File(cfg.root)
        if (!rootDir.exists() || !rootDir.isDirectory) {
            logHealth("ERROR: Root directory does not exist: ${cfg.root}")
            closeLoggers()
            exitProcess(1)
        }

        // 1. Snapshot baseline files before starting watchers (Section 6.3)
        buildBaselineSnapshot(rootDir)

        // 2. Recursively register FileObservers on all existing directories (Section 6.2)
        registerRecursiveWatchers(rootDir)

        logHealth(
            "STARTUP root=${cfg.root} baseline_files=${baselineFiles.size} " +
            "watchers=${observers.size} window_ms=${cfg.windowMs} " +
            "min_files=${cfg.minFiles} min_score=${cfg.minScore} kill=${cfg.kill}"
        )
        emitStdoutJson(
            """{"ts_ns":${nowNs()},"kind":"health","status":"started","root":"${jsonEscape(cfg.root)}","baseline_files":${baselineFiles.size},"watchers":${observers.size}}"""
        )

        for (d in observers.keys.sorted()) logHealth("WATCHED ${toRelPath(d).ifEmpty { "." }}")
        ready = true

        // 3. Process events on main worker thread (Section 6.2)
        while (running.get()) {
            val ev = rawQueue.poll(100, TimeUnit.MILLISECONDS)
            if (ev != null) {
                processRawEvent(ev)
            } else {
                evictExpiredWindow(nowNs())
            }
        }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        for ((_, obs) in observers) {
            try {
                obs.stopWatching()
            } catch (_: Throwable) {}
        }
        observers.clear()
        logHealth("STOPPED cleanly stdout_dropped=$stdoutDropped")
        closeLoggers()
    }

    // -----------------------------------------------------------------------
    // Setup: Loggers, Baseline Snapshot, Recursive Watchers
    // -----------------------------------------------------------------------
    private fun initLoggers() {
        val out = File(cfg.outDir)
        out.mkdirs()
        File(cfg.registryDir).mkdirs()

        eventsWriter = BufferedWriter(OutputStreamWriter(FileOutputStream(File(out, "events.jsonl"), true), Charsets.UTF_8))
        alertsWriter = BufferedWriter(OutputStreamWriter(FileOutputStream(File(out, "alerts.jsonl"), true), Charsets.UTF_8))
        killWriter = BufferedWriter(OutputStreamWriter(FileOutputStream(File(out, "kill.jsonl"), true), Charsets.UTF_8))
        healthWriter = BufferedWriter(OutputStreamWriter(FileOutputStream(File(out, "health.log"), true), Charsets.UTF_8))
    }

    private fun closeLoggers() {
        try { eventsWriter.flush(); eventsWriter.close() } catch (_: Throwable) {}
        try { alertsWriter.flush(); alertsWriter.close() } catch (_: Throwable) {}
        try { killWriter.flush(); killWriter.close() } catch (_: Throwable) {}
        try { healthWriter.flush(); healthWriter.close() } catch (_: Throwable) {}
    }

    private fun buildBaselineSnapshot(rootDir: File) {
        baselineFiles.clear()
        knownExtensions.clear()
        knownExtensions.addAll(listOf("", "jpg", "jpeg", "png", "pdf", "docx", "txt", "csv", "zip", "mp3", "mp4", "vcf", "json", "db"))

        rootDir.walkTopDown().forEach { f ->
            if (f.isFile) {
                val rel = toRelPath(f.absolutePath)
                if (rel.isNotEmpty() && !isExcludedPath(rel)) {
                    baselineFiles[rel] = f.length()
                    val ext = extOf(f.name)
                    if (ext.isNotEmpty()) knownExtensions.add(ext)
                }
            }
        }
    }

    private fun registerRecursiveWatchers(startDir: File) {
        startDir.walkTopDown().forEach { d ->
            if (d.isDirectory) {
                val rel = toRelPath(d.absolutePath)
                if (!isExcludedPath(rel)) {
                    addDirectoryWatcher(d.absolutePath)
                }
            }
        }
    }

    private fun addDirectoryWatcher(dirPath: String, synthesizeExistingFiles: Boolean = false) {
        val cleanDir = dirPath.trimEnd('/').ifEmpty { "/" }
        if (observers.containsKey(cleanDir)) return
        val dirFile = File(cleanDir)
        if (!dirFile.isDirectory) return

        @Suppress("DEPRECATION")
        val obs = object : FileObserver(cleanDir, WATCH_MASK) {
            override fun onEvent(event: Int, path: String?) {
                // Keep callback ultra-fast: filter null/0, enqueue to worker thread (Section 6.2 & 6.4)
                val masked = event and FileObserver.ALL_EVENTS
                if (masked == 0 || path.isNullOrEmpty()) return
                rawQueue.offer(RawFsEvent(nowNs(), event, cleanDir, path))
            }
        }
        val prev = observers.putIfAbsent(cleanDir, obs)
        if (prev == null) {
            obs.startWatching()
            // Race condition guard: if a new subdirectory was just created (e.g. B1 mkdir + immediate cp),
            // synthesize CREATE events for any files or subdirectories created before startWatching() completed.
            if (synthesizeExistingFiles) {
                val children = dirFile.listFiles() ?: return
                val ts = nowNs()
                for (child in children) {
                    if (child.isDirectory) {
                        addDirectoryWatcher(child.absolutePath, synthesizeExistingFiles = true)
                    } else if (child.isFile) {
                        rawQueue.offer(RawFsEvent(ts, FileObserver.CREATE, cleanDir, child.name))
                    }
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Event Normalisation & Noise Filtering (Section 6.4)
    // -----------------------------------------------------------------------
    private fun processRawEvent(ev: RawFsEvent) {
        private fun processRawEvent(ev: RawFsEvent) {
        // Always-on daemon guard: if host is resetting /sdcard dataset, ignore push events
        if (File(cfg.registryDir, ".resetting").exists()) {
            windowActions.clear()
            renamedBaselineMap.clear()
            return
        }
        val masked = ev.mask and FileObserver.ALL_EVENTS
        if (masked == 0 || ev.relName.isEmpty()) return
        if (ev.relName.startsWith(PROBE_PREFIX)) { probeSeen[ev.dirPath] = true; return }

        val fullPath = "${ev.dirPath}/${ev.relName}"
        val relPath = toRelPath(fullPath)
        if (isExcludedPath(relPath)) return

        val isDirEvent = (ev.mask and IN_ISDIR) != 0 || File(fullPath).isDirectory

        // Dynamic directory watching (Section 6.2)
        if (isDirEvent) {
            if ((masked and (FileObserver.CREATE or FileObserver.MOVED_TO)) != 0) {
                addDirectoryWatcher(fullPath, synthesizeExistingFiles = true)
            } else if ((masked and FileObserver.DELETE) != 0) {
                observers.remove(fullPath)?.stopWatching()
            }
            return
        }

        // Check if this path is a known baseline file (or a previously renamed baseline file)
        val baselineKey = resolveBaselineKey(relPath)
        val isBaseline = baselineKey != null

        // Determine individual event bit(s)
        val eventTypes = mutableListOf<Int>()
        if ((masked and FileObserver.CREATE) != 0) eventTypes.add(FileObserver.CREATE)
        if ((masked and FileObserver.MODIFY) != 0) eventTypes.add(FileObserver.MODIFY)
        if ((masked and FileObserver.CLOSE_WRITE) != 0) eventTypes.add(FileObserver.CLOSE_WRITE)
        if ((masked and FileObserver.MOVED_FROM) != 0) eventTypes.add(FileObserver.MOVED_FROM)
        if ((masked and FileObserver.MOVED_TO) != 0) eventTypes.add(FileObserver.MOVED_TO)
        if ((masked and FileObserver.DELETE) != 0) eventTypes.add(FileObserver.DELETE)

        for (t in eventTypes) {
            // Noise filter 1: Suppress stray MODIFY right after MOVED_TO on /sdcard FUSE path (Section 6.4)
            if (t == FileObserver.MODIFY) {
                val movedTs = lastMovedToNs[relPath]
                if (movedTs != null && (ev.tsNs - movedTs) in 0..POST_MOVE_SUPPRESS_NS) {
                    continue
                }
            }

            val typeStr = eventTypeName(t)

            // Log raw normalized event to events.jsonl and stdout (Section 6.7)
            logEvent(ev.tsNs, typeStr, fullPath, isBaseline)

            // Ignore temporary helper files (.tmp, -journal, -wal) for scoring EXCEPT when moved onto a baseline file
            if (isBenignTempFile(ev.relName) && t != FileObserver.MOVED_TO) {
                if (t == FileObserver.MOVED_FROM) {
                    // Record temp source so MOVED_TO knows it came from an atomic save (.tmp -> baseline)
                    pendingMovedFromPath = relPath
                    pendingMovedFromBaseKey = null
                    pendingMovedFromTsNs = ev.tsNs
                }
                continue
            }

            // Route to sliding-window detector (Section 6.4 & 6.5)
            when (t) {
                FileObserver.CREATE -> handleCreate(ev.tsNs, relPath, fullPath, baselineKey)
                FileObserver.MODIFY, FileObserver.CLOSE_WRITE -> handleWrite(ev.tsNs, relPath, fullPath, baselineKey)
                FileObserver.MOVED_FROM -> handleMovedFrom(ev.tsNs, relPath, baselineKey)
                FileObserver.MOVED_TO -> handleMovedTo(ev.tsNs, relPath, fullPath, baselineKey)
                FileObserver.DELETE -> handleDelete(ev.tsNs, relPath, fullPath, baselineKey)
            }
        }
    }

    private fun handleCreate(tsNs: Long, relPath: String, fullPath: String, baselineKey: String?) {
        if (baselineKey != null) {
            // Recreating an existing baseline file path counts as overwriting that baseline file
            handleWrite(tsNs, relPath, fullPath, baselineKey)
            return
        }

        // Check if this new file is a companion encrypted copy of a baseline file (e.g. A2 creates file.enc)
        val companionBaseKey = findCompanionBaselineFile(relPath)
        val ext = extOf(relPath)
        val fileNameLower = File(relPath).name.lowercase()
        val isRansomNote = RANSOM_NOTE_KEYWORDS.any { fileNameLower.contains(it) }

        if (companionBaseKey != null && (ext in SUSPICIOUS_EXTENSIONS || ext !in knownExtensions)) {
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.EXT_CHANGE,
                    weight = 3.0,
                    baselineKey = companionBaseKey,
                    path = fullPath,
                    reason = "companion_encrypted_create:$ext"
                )
            )
        } else {
            val weight = if (isRansomNote) 2.0 else 0.5
            val reason = if (isRansomNote) "ransom_note_create" else "new_file_create"
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.NEW_FILE_CREATE,
                    weight = weight,
                    baselineKey = null,
                    path = fullPath,
                    reason = reason
                )
            )
        }
    }

    private fun handleWrite(tsNs: Long, relPath: String, fullPath: String, baselineKey: String?) {
        // Collapse MODIFY + CLOSE_WRITE bursts on the same path within 200ms into 1 write (Section 6.4)
        val lastWrite = lastWriteCollapsedNs[relPath]
        if (lastWrite != null && (tsNs - lastWrite) in 0..WRITE_COLLAPSE_NS) {
            return
        }
        lastWriteCollapsedNs[relPath] = tsNs

        if (baselineKey != null) {
            val origSize = baselineFiles[baselineKey]
            val curSize = File(fullPath).length()
            val reason = if (origSize != null && origSize > 0L && curSize == origSize) {
                "baseline_write_size_preserved"
            } else {
                "baseline_write"
            }
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.BASELINE_WRITE,
                    weight = 3.0,
                    baselineKey = baselineKey,
                    path = fullPath,
                    reason = reason
                )
            )
        }
    }

    private fun handleMovedFrom(tsNs: Long, relPath: String, baselineKey: String?) {
        pendingMovedFromPath = relPath
        pendingMovedFromBaseKey = baselineKey
        pendingMovedFromTsNs = tsNs
    }

    private fun handleMovedTo(tsNs: Long, relPath: String, fullPath: String, targetBaselineKey: String?) {
        lastMovedToNs[relPath] = tsNs

        val fromPath = pendingMovedFromPath
        val fromBaseKey = pendingMovedFromBaseKey
        val pairValid = fromPath != null && (tsNs - pendingMovedFromTsNs) in 0..RENAME_PAIR_NS

        pendingMovedFromPath = null
        pendingMovedFromBaseKey = null

        if (pairValid && fromPath != null) {
            // Case 1: Benign/atomic save (e.g. B2: mv journal_1.txt.tmp -> journal_1.txt)
            if (isBenignTempFile(fromPath) && targetBaselineKey != null) {
                // Treat atomic replacement of baseline file as a single write on that baseline file
                lastWriteCollapsedNs[relPath] = tsNs
                recordAction(
                    ScoredAction(
                        tsNs = tsNs,
                        kind = ActionKind.BASELINE_WRITE,
                        weight = 3.0,
                        baselineKey = targetBaselineKey,
                        path = fullPath,
                        reason = "atomic_save_write"
                    )
                )
                return
            }

            // Case 2: A baseline file was renamed (e.g. A3: mv IMG.jpg -> IMG.jpg.locked)
            if (fromBaseKey != null) {
                renamedBaselineMap[relPath] = fromBaseKey
                val oldExt = extOf(fromPath)
                val newExt = extOf(relPath)
                val extChanged = oldExt != newExt
                val reason = if (extChanged) "baseline_rename_ext:$oldExt->$newExt" else "baseline_rename"

                recordAction(
                    ScoredAction(
                        tsNs = tsNs,
                        kind = if (extChanged) ActionKind.EXT_CHANGE else ActionKind.BASELINE_RENAME,
                        weight = 3.0,
                        baselineKey = fromBaseKey,
                        path = fullPath,
                        reason = reason
                    )
                )
                return
            }
        }

        // Case 3: Unpaired MOVED_TO or non-baseline rename with suspicious extension
        val ext = extOf(relPath)
        if (targetBaselineKey != null) {
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.BASELINE_WRITE,
                    weight = 3.0,
                    baselineKey = targetBaselineKey,
                    path = fullPath,
                    reason = "moved_to_baseline"
                )
            )
        } else if (ext in SUSPICIOUS_EXTENSIONS || ext !in knownExtensions) {
            val companion = findCompanionBaselineFile(relPath)
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.EXT_CHANGE,
                    weight = 3.0,
                    baselineKey = companion,
                    path = fullPath,
                    reason = "suspicious_move_ext:$ext"
                )
            )
        }
    }

    private fun handleDelete(tsNs: Long, relPath: String, fullPath: String, baselineKey: String?) {
        if (baselineKey != null) {
            recordAction(
                ScoredAction(
                    tsNs = tsNs,
                    kind = ActionKind.BASELINE_DELETE,
                    weight = 2.0,
                    baselineKey = baselineKey,
                    path = fullPath,
                    reason = "baseline_delete"
                )
            )
        }
    }

    // -----------------------------------------------------------------------
    // Sliding-Window Detector (Section 6.5)
    // -----------------------------------------------------------------------
    private fun recordAction(action: ScoredAction) {
        windowActions.addLast(action)
        evictExpiredWindow(action.tsNs)
        evaluateWindow(action.tsNs)
    }

    private fun evictExpiredWindow(nowNs: Long) {
        val cutoffNs = nowNs - (cfg.windowMs * 1_000_000L)
        while (windowActions.isNotEmpty() && windowActions.first().tsNs < cutoffNs) {
            windowActions.removeFirst()
        }
    }

    private fun evaluateWindow(nowNs: Long) {
        if (windowActions.isEmpty()) return
        if (nowNs - lastAlertTsNs < cfg.cooldownMs * 1_000_000L) return

        val distinctBaselineTouched = HashSet<String>()
        var totalScore = 0.0
        val reasonCounts = LinkedHashMap<String, Int>()

        for (act in windowActions) {
            totalScore += act.weight
            if (act.baselineKey != null) {
                distinctBaselineTouched.add(act.baselineKey)
            }
            reasonCounts[act.reason] = (reasonCounts[act.reason] ?: 0) + 1
        }

        val filesTouchedCount = distinctBaselineTouched.size
        if (filesTouchedCount >= cfg.minFiles && totalScore >= cfg.minScore) {
            lastAlertTsNs = nowNs
            val reasonsList = reasonCounts.entries.map { "${it.key}(${it.value})" }
            triggerAlertAndKill(
                alertTsNs = nowNs,
                score = totalScore,
                filesTouched = filesTouchedCount,
                reasons = reasonsList
            )
            // Clear window after firing an alert+kill so a new trial/demo in the same session starts fresh
            windowActions.clear()
        }
    }

    // -----------------------------------------------------------------------
    // Attribution & Process Kill via /proc & Registry (Section 6.6)
    // -----------------------------------------------------------------------
    private fun triggerAlertAndKill(
        alertTsNs: Long,
        score: Double,
        filesTouched: Int,
        reasons: List<String>
    ) {
        val candidates = attributeCandidates()
        val chosen = candidates.maxByOrNull { it.activityScore() }

        // 1. Emit alert log (Section 6.7)
        val reasonsJson = reasons.joinToString(",") { "\"${jsonEscape(it)}\"" }
        val candidatesJson = candidates.joinToString(",") {
            """{"name":"${jsonEscape(it.name)}","pid":${it.pid},"wchar_delta":${it.wcharDelta},"total_wchar":${it.totalWchar},"open_fds":${it.openWatchedFds}}"""
        }
        val alertLine =
            """{"ts_ns":$alertTsNs,"kind":"alert","score":$score,"files_touched":$filesTouched,"window_ms":${cfg.windowMs},"reasons":[$reasonsJson],"candidates":[$candidatesJson]}"""

        emitStdoutJson(alertLine)
        synchronized(alertsWriter) {
            alertsWriter.write(alertLine)
            alertsWriter.newLine()
            alertsWriter.flush()
        }

        if (!cfg.kill) return

        // 2. Kill only the attributed workload PID (plus any in-flight child command it spawned)
        if (chosen == null || chosen.activityScore() <= 0L) {
            val killTs = nowNs()
            val killLine = """{"ts_ns":$killTs,"kind":"kill","name":"none","pid":-1,"result":"unattributed"}"""
            emitStdoutJson(killLine)
            writeKillLog(killLine)
            return
        }

        val targetPid = chosen.pid
        val targetName = chosen.name
        var killSuccess = false

        try {
            // Also snapshot direct child PIDs (e.g. in-flight `head`, `cp`, `mv`) before killing parent shell
            val children = findDirectChildren(targetPid)
            AndroidProcess.killProcess(targetPid)
            for (childPid in children) {
                try { AndroidProcess.killProcess(childPid) } catch (_: Throwable) {}
            }
            // Wait up to 50ms to confirm /proc/<pid> disappears or turns zombie
            for (attempt in 1..5) {
                if (! isProcessAlive(targetPid)) {
                    killSuccess = true
                    break
                }
                Thread.sleep(10)
            }
            if (!killSuccess) {
                // Fallback kill -9
                ProcessBuilder("kill", "-9", targetPid.toString()).start().waitFor(1, TimeUnit.SECONDS)
                killSuccess = !isProcessAlive(targetPid)
            }
        } catch (e: Throwable) {
            logHealth("ERROR killing pid=$targetPid ($targetName): ${e.message}")
        }

        val killTsNs = nowNs()
        val resultStr = if (killSuccess) "ok" else "failed"
        val killLine = """{"ts_ns":$killTsNs,"kind":"kill","name":"${jsonEscape(targetName)}","pid":$targetPid,"result":"$resultStr"}"""
        emitStdoutJson(killLine)
        writeKillLog(killLine)
    }

    private fun attributeCandidates(): List<CandidateProc> {
        val regDir = File(cfg.registryDir)
        val pidFiles = regDir.listFiles { f -> f.isFile && f.name.endsWith(".pid") } ?: return emptyList()

        data class Snap(
            val name: String,
            val pid: Int,
            val cmdline: String,
            val wchar1: Long,
            val syscw1: Long,
            val fds1: Int,
            val children1: List<Int>
        )

        val firstSnaps = mutableListOf<Snap>()
        val myPid = AndroidProcess.myPid()

        for (pf in pidFiles) {
            val name = pf.name.removeSuffix(".pid")
            val pid = try { pf.readText().trim().toIntOrNull() } catch (_: Throwable) { null } ?: continue
            if (pid <= 1 || pid == myPid) continue

            val procDir = File("/proc/$pid")
            if (!procDir.exists()) {
                // Stale registry entry (Section 6.6 step 1)
                continue
            }

            val cmdline = readProcCmdline(pid)
            if (!isExpectedWorkloadCmdline(cmdline)) {
                continue
            }

            val children = findDirectChildren(pid)
            val (w1, s1) = readProcIoWithChildren(pid, children)
            val fds1 = countOpenWatchedFds(pid, children)
            firstSnaps.add(Snap(name, pid, cmdline, w1, s1, fds1, children))
        }

        if (firstSnaps.isEmpty()) return emptyList()

        // Take a second quick sample 25ms later to measure live wchar/syscw delta (Section 6.6 step 2)
        try { Thread.sleep(25) } catch (_: Throwable) {}

        val results = mutableListOf<CandidateProc>()
        for (s in firstSnaps) {
            val children2 = findDirectChildren(s.pid)
            val allChildren = (s.children1 + children2).distinct()
            val (w2, sc2) = readProcIoWithChildren(s.pid, allChildren)
            val fds2 = maxOf(s.fds1, countOpenWatchedFds(s.pid, allChildren))
            results.add(
                CandidateProc(
                    name = s.name,
                    pid = s.pid,
                    cmdline = s.cmdline,
                    openWatchedFds = fds2,
                    wcharDelta = (w2 - s.wchar1).coerceAtLeast(0L),
                    syscwDelta = (sc2 - s.syscw1).coerceAtLeast(0L),
                    totalWchar = w2,
                    totalSyscw = sc2,
                    activeChildren = allChildren
                )
            )
        }
        return results
    }

    // Reads /proc/<pid>/io (plus any live child processes) for wchar and syscw
    private fun readProcIoWithChildren(pid: Int, children: List<Int>): Pair<Long, Long> {
        var totalWchar = 0L
        var totalSyscw = 0L
        val pids = listOf(pid) + children
        for (p in pids) {
            try {
                val lines = File("/proc/$p/io").readLines()
                for (line in lines) {
                    if (line.startsWith("wchar:")) {
                        totalWchar += line.substringAfter("wchar:").trim().toLongOrNull() ?: 0L
                    } else if (line.startsWith("syscw:")) {
                        totalSyscw += line.substringAfter("syscw:").trim().toLongOrNull() ?: 0L
                    }
                }
            } catch (_: Throwable) {}
        }
        return Pair(totalWchar, totalSyscw)
    }

    // Scans /proc/<pid>/fd (and children) for open descriptors in /data/media/0, /sdcard, or /storage/emulated/0
    private fun countOpenWatchedFds(pid: Int, children: List<Int>): Int {
        var hits = 0
        val prefixes = listOf(
            cfg.root,
            "/sdcard",
            "/storage/emulated/0",
            "/data/media/0"
        ).distinct()

        for (p in listOf(pid) + children) {
            val fdDir = File("/proc/$p/fd")
            val entries = fdDir.listFiles() ?: continue
            for (fd in entries) {
                try {
                    val target = fd.canonicalPath
                    if (prefixes.any { target.startsWith(it) }) {
                        hits++
                    }
                } catch (_: Throwable) {}
            }
        }
        return hits
    }

    private fun findDirectChildren(parentPid: Int): List<Int> {
        val children = mutableListOf<Int>()
        try {
            // Fast check via /proc/<pid>/task/<pid>/children if enabled in kernel
            val childrenFile = File("/proc/$parentPid/task/$parentPid/children")
            if (childrenFile.canRead()) {
                val txt = childrenFile.readText().trim()
                if (txt.isNotEmpty()) {
                    return txt.split(Regex("\\s+")).mapNotNull { it.toIntOrNull() }
                }
            }
            // Fallback: scan /proc/[0-9]*/stat for PPid (field 4 after comm)
            val procEntries = File("/proc").listFiles() ?: return emptyList()
            for (entry in procEntries) {
                val pid = entry.name.toIntOrNull() ?: continue
                if (pid <= 1 || pid == parentPid) continue
                val stat = File(entry, "stat")
                if (!stat.canRead()) continue
                val content = stat.readText()
                val closeParen = content.lastIndexOf(')')
                if (closeParen > 0 && closeParen + 2 < content.length) {
                    val fields = content.substring(closeParen + 2).trim().split(' ')
                    val ppid = fields.getOrNull(1)?.toIntOrNull()
                    if (ppid == parentPid) children.add(pid)
                }
            }
        } catch (_: Throwable) {}
        return children
    }

    private fun readProcCmdline(pid: Int): String {
        return try {
            val bytes = File("/proc/$pid/cmdline").readBytes()
            bytes.map { if (it == 0.toByte()) ' ' else it.toInt().toChar() }.joinToString("").trim()
        } catch (_: Throwable) {
            ""
        }
    }

    // Guards against PID reuse by ensuring the process is an sh/toybox/workload process (Section 6.6 step 3)
    private fun isExpectedWorkloadCmdline(cmdline: String): Boolean {
        if (cmdline.isEmpty()) return false
        val lower = cmdline.lowercase()
        if (lower.contains("app_process") || lower.contains("zygote") || lower.contains("system_server")) {
            return false
        }
        return lower.contains("sh") || lower.contains("workload") || lower.contains("/data/local/tmp") ||
               lower.contains("a1") || lower.contains("a2") || lower.contains("a3") ||
               lower.contains("b1") || lower.contains("b2") || lower.contains("selftest")
    }

    private fun isProcessAlive(pid: Int): Boolean {
        val statFile = File("/proc/$pid/stat")
        if (!statFile.exists()) return false
        return try {
            val content = statFile.readText()
            val closeParen = content.lastIndexOf(')')
            val state = content.substring(closeParen + 2).trim().firstOrNull()
            state != null && state != 'Z' && state != 'X'
        } catch (_: Throwable) {
            false
        }
    }

    // -----------------------------------------------------------------------
    // Path & Logging Helpers
    // -----------------------------------------------------------------------
    private fun toRelPath(absPath: String): String {
        val clean = absPath.replace("//", "/")
        return if (clean == cfg.root) ""
        else if (clean.startsWith(cfg.root + "/")) clean.substring(cfg.root.length + 1)
        else clean.trimStart('/')
    }

    private fun isExcludedPath(relPath: String): Boolean {
        if (relPath.isEmpty()) return false
        // Ignore Android's internal non-media folders except Android/media (where WhatsApp dataset lives)
        if (relPath == "Android/data" || relPath.startsWith("Android/data/") ||
            relPath == "Android/obb" || relPath.startsWith("Android/obb/") ||
            relPath.startsWith(".trashed") || relPath.startsWith(".pending")) {
            return true
        }
        return false
    }

    private fun isBenignTempFile(pathOrName: String): Boolean {
        val lower = pathOrName.lowercase()
        return BENIGN_TEMP_SUFFIXES.any { lower.endsWith(it) }
    }

    private fun resolveBaselineKey(relPath: String): String? {
        if (baselineFiles.containsKey(relPath)) return relPath
        return renamedBaselineMap[relPath]
    }

    private fun findCompanionBaselineFile(newRelPath: String): String? {
        // e.g. A2 creates "DCIM/Camera/IMG_01.jpg.enc" next to baseline "DCIM/Camera/IMG_01.jpg"
        val dot = newRelPath.lastIndexOf('.')
        if (dot <= 0) return null
        val stripped = newRelPath.substring(0, dot)
        if (baselineFiles.containsKey(stripped)) return stripped
        return null
    }

    private fun extOf(path: String): String {
        val name = path.substringAfterLast('/')
        val dot = name.lastIndexOf('.')
        return if (dot > 0 && dot < name.length - 1) name.substring(dot + 1).lowercase() else ""
    }

    private fun eventTypeName(t: Int): String = when (t) {
        FileObserver.CREATE -> "CREATE"
        FileObserver.MODIFY -> "MODIFY"
        FileObserver.CLOSE_WRITE -> "CLOSE_WRITE"
        FileObserver.MOVED_FROM -> "MOVED_FROM"
        FileObserver.MOVED_TO -> "MOVED_TO"
        FileObserver.DELETE -> "DELETE"
        else -> "OTHER($t)"
    }

    private fun logEvent(tsNs: Long, typeStr: String, fullPath: String, isBaseline: Boolean) {
        val line =
            """{"ts_ns":$tsNs,"kind":"event","type":"$typeStr","path":"${jsonEscape(fullPath)}","baseline":$isBaseline}"""
        emitStdoutJson(line)
        synchronized(eventsWriter) {
            eventsWriter.write(line)
            eventsWriter.newLine()
            eventsWriter.flush()
        }
    }

    private fun writeKillLog(line: String) {
        synchronized(killWriter) {
            killWriter.write(line)
            killWriter.newLine()
            killWriter.flush()
        }
        // Also mirror kill record into alerts.jsonl so single-file log parsers see it
        synchronized(alertsWriter) {
            alertsWriter.write(line)
            alertsWriter.newLine()
            alertsWriter.flush()
        }
    }

    private fun logHealth(msg: String) {
        val line = "[${Instant.now()}] $msg"
        synchronized(healthWriter) {
            healthWriter.write(line)
            healthWriter.newLine()
            healthWriter.flush()
        }
    }

    private fun emitStdoutJson(jsonLine: String) {
        if (!stdoutQueue.offer(jsonLine)) {
            stdoutDropped++
            // alert/kill lines jump the queue; plain events are dropped (they are still in events.jsonl)
            if (jsonLine.contains("\"kind\":\"alert\"") || jsonLine.contains("\"kind\":\"kill\"")) {
                stdoutQueue.poll()
                stdoutQueue.offer(jsonLine)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Built-in Open Checks & End-to-End Verification Suite (Sections 8 & 9)
// ---------------------------------------------------------------------------
private fun runEnvironmentChecks(): Boolean {
    println("=== PS-001 Open Checks & Environment Verification ===")
    var allOk = true

    fun report(name: String, pass: Boolean, detail: String) {
        println("${if (pass) "PASS" else "FAIL"}  $name   [$detail]")
        if (!pass) allOk = false
    }

    // 1. Root UID check
    val uid = AndroidProcess.myUid()
    report("Root privileges (uid=0)", uid == 0, "uid=$uid")

    // 2. Open Check #1: FUSE (/sdcard) write -> /data/media/0 FileObserver visibility
    val rawTestDir = File("/data/media/0/radar_fuse_check")
    val fuseTestDir = File("/sdcard/radar_fuse_check")
    rawTestDir.deleteRecursively()
    rawTestDir.mkdirs()

    val fuseSeen = LinkedBlockingQueue<String>()
    @Suppress("DEPRECATION")
    val obs = object : FileObserver(
        rawTestDir.absolutePath,
        FileObserver.CREATE or FileObserver.MODIFY or FileObserver.CLOSE_WRITE or
        FileObserver.MOVED_FROM or FileObserver.MOVED_TO or FileObserver.DELETE
    ) {
        override fun onEvent(event: Int, path: String?) {
            val m = event and FileObserver.ALL_EVENTS
            if (m != 0 && !path.isNullOrEmpty()) fuseSeen.offer("$m:$path")
        }
    }
    obs.startWatching()
    Thread.sleep(200)
    // Write via /sdcard (FUSE) while watching /data/media/0 (lower FS)
    val cmd = "echo test123 > ${fuseTestDir.absolutePath}/f.txt; mv ${fuseTestDir.absolutePath}/f.txt ${fuseTestDir.absolutePath}/f.locked; rm ${fuseTestDir.absolutePath}/f.locked"
    ProcessBuilder("sh", "-c", cmd).start().waitFor(3, TimeUnit.SECONDS)
    Thread.sleep(400)
    obs.stopWatching()
    rawTestDir.deleteRecursively()

    val seenList = mutableListOf<String>()
    fuseSeen.drainTo(seenList)
    report(
        "Open Check #1: /sdcard (FUSE) writes seen on /data/media/0 watcher",
        seenList.isNotEmpty(),
        "events_count=${seenList.size}, sample=${seenList.take(6).joinToString(",")}"
    )

    // 3. Open Check #2: fanotify support in emulator kernel
    val fanotifyProc = ProcessBuilder("sh", "-c", "zcat /proc/config.gz 2>/dev/null | grep FANOTIFY || echo NONE").start()
    val fanotifyOut = fanotifyProc.inputStream.bufferedReader().readText().trim().replace("\n", "; ")
    fanotifyProc.waitFor(2, TimeUnit.SECONDS)
    println("INFO  Open Check #2 (fanotify kernel config)   [$fanotifyOut]")

    // 4. Open Check #5: Teammate shell toolbox (date +%s%N, sleep 0.1, stat -c %s, /dev/urandom)
    val tbProc = ProcessBuilder("sh", "-c", "date +%s%N && sleep 0.1 && head -c 16 /dev/urandom | wc -c").start()
    val tbLines = tbProc.inputStream.bufferedReader().readLines().map { it.trim() }
    tbProc.waitFor(3, TimeUnit.SECONDS)
    val nsValid = tbLines.firstOrNull()?.length ?: 0 >= 18
    val urandomValid = tbLines.lastOrNull() == "16"
    report(
        "Open Check #5: Shell toolbox (date +%s%N, sleep 0.1, /dev/urandom)",
        nsValid && urandomValid,
        "date_ns=${tbLines.firstOrNull()}, urandom_bytes=${tbLines.lastOrNull()}"
    )

    return allOk
}

private fun runEndToEndSelfTest(): Boolean {
    if (!runEnvironmentChecks()) return false
    println("\n=== Running End-to-End Monitor + Attribution + Kill Self-Test ===")

    val testRoot = File("/data/media/0/radar_e2e_test")
    val testReg = File("/data/local/tmp/radar_e2e_reg")
    val testLogs = File("/data/local/tmp/radar_e2e_logs")
    testRoot.deleteRecursively()
    testReg.deleteRecursively()
    testLogs.deleteRecursively()
    testRoot.mkdirs()
    testReg.mkdirs()
    testLogs.mkdirs()

    // Create 15 dummy baseline files in subfolders
    val docsDir = File(testRoot, "Documents")
    docsDir.mkdirs()
    for (i in 1..15) {
        File(docsDir, "doc_$i.txt").writeText("Baseline content for document $i\n".repeat(20))
    }

    val cfg = Config(
        root = testRoot.absolutePath,
        windowMs = 5000L,
        minFiles = 6,
        minScore = 15.0,
        kill = true,
        registryDir = testReg.absolutePath,
        outDir = testLogs.absolutePath
    )
    val monitor = RadarMonitor(cfg)
    val monThread = Thread { monitor.start() }
    monThread.isDaemon = true
    monThread.start()
    Thread.sleep(600) // allow baseline snapshot + watchers to initialize

    // 1. Benign B1-style test: create 10 NEW files in a new subfolder -> MUST NOT ALERT
    val backupDir = File(testRoot, "Backups/run_test")
    backupDir.mkdirs()
    val b1Cmd = "for i in 1 2 3 4 5 6 7 8 9 10; do echo copy > ${backupDir.absolutePath}/new_\$i.jpg; sleep 0.05; done"
    ProcessBuilder("sh", "-c", b1Cmd).start().waitFor(5, TimeUnit.SECONDS)
    Thread.sleep(400)

    val alertsAfterB1 = File(testLogs, "alerts.jsonl").let { if (it.exists()) it.readLines().size else 0 }
    val b1Pass = alertsAfterB1 == 0
    println("${if (b1Pass) "PASS" else "FAIL"}  Benign B1 simulation (10 new files in new subfolder) -> 0 alerts   [alerts=$alertsAfterB1]")

    // 2. Attack A3-style test: spawn registered attacker touching 15 baseline files with 0.1s delay -> MUST ALERT & KILL at ~6 files
    val attackerScript = File(testReg, "selftest_a3.sh")
    attackerScript.writeText(
        """
        #!/system/bin/sh
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            f="${docsDir.absolutePath}/doc_${'$'}i.txt"
            head -c 512 /dev/urandom > "${'$'}f"
            mv "${'$'}f" "${'$'}f.locked"
            sleep 0.1
        done
        """.trimIndent() + "\n"
    )
    val proc = ProcessBuilder("sh", "-c", "echo \$\$; exec sh ${attackerScript.absolutePath}").start()
    val attackerPid = proc.inputStream.bufferedReader().readLine()?.trim()?.toIntOrNull() ?: -1
    File(testReg, "SELFTEST_A3.pid").writeText("$attackerPid\n")

    val finishedWait = proc.waitFor(5, TimeUnit.SECONDS)
    Thread.sleep(400)
    monitor.stop()

    val killLog = File(testLogs, "kill.jsonl").let { if (it.exists()) it.readText().trim() else "" }
    val lockedFiles = docsDir.listFiles { f -> f.name.endsWith(".locked") }?.size ?: 0
    val survivingFiles = docsDir.listFiles { f -> f.name.endsWith(".txt") }?.size ?: 0

    val killPass = finishedWait && killLog.contains("\"result\":\"ok\"") && lockedFiles in 6..9 && survivingFiles >= 6
    println(
        "${if (killPass) "PASS" else "FAIL"}  Attack A3 simulation alerted & killed attacker PID $attackerPid   " +
        "[locked_before_kill=$lockedFiles/15, surviving_intact=$survivingFiles/15, kill_log=$killLog]"
    )

    testRoot.deleteRecursively()
    testReg.deleteRecursively()
    testLogs.deleteRecursively()
    return b1Pass && killPass
}

// ---------------------------------------------------------------------------
// --check-dirs: proves the monitor really observes the dataset directories
// ---------------------------------------------------------------------------
private fun runDirCheck(base: Config): Boolean {
    println("=== PS-001 watched-directory verification ===")
    println("monitor root : ${base.root}")
    println("probe path   : ${base.probeRoot}   (probe files are written through this path, the way the workloads write)")

    val cfg = base.copy(kill = false, outDir = base.outDir.trimEnd('/') + "_dircheck")
    val monitor = RadarMonitor(cfg)
    val t = Thread { monitor.start() }
    t.isDaemon = true
    t.start()
    var waited = 0
    while (!monitor.ready && waited < 20_000) { Thread.sleep(100); waited += 100 }
    if (!monitor.ready) { println("FAIL  monitor did not start within 20 s"); return false }
    Thread.sleep(300)

    var ok = true
    val dirs = monitor.watchedDirs()
    println("watchers registered: ${dirs.size}")

    // 1. every expected dataset directory must exist and be watched
    for (rel in base.expectDirs) {
        val abs = "${base.root}/$rel"
        val exists = File(abs).isDirectory
        val watched = dirs.contains(abs)
        val pass = exists && watched
        println("${if (pass) "PASS" else "FAIL"}  expected dir is watched: $rel   [exists=$exists watched=$watched]")
        if (!pass) ok = false
    }

    // 2. every watched dir: create+delete a hidden probe file through probeRoot; the watcher must report it
    var confirmed = 0
    val failed = mutableListOf<String>()
    for (dir in dirs) {
        val rel = monitor.relOf(dir)
        val via = if (rel.isEmpty()) base.probeRoot else "${base.probeRoot}/$rel"
        val name = "${RadarMonitor.PROBE_PREFIX}${System.nanoTime()}"
        monitor.probeSeen.remove(dir)
        val p = ProcessBuilder("sh", "-c", "echo p > \"\$1/\$2\" && rm -f \"\$1/\$2\"", "sh", via, name)
            .redirectErrorStream(true).start()
        val shellOut = p.inputStream.bufferedReader().readText().trim()
        p.waitFor(3, TimeUnit.SECONDS)
        var seen = false
        for (k in 1..50) { if (monitor.probeSeen[dir] == true) { seen = true; break }; Thread.sleep(20) }
        if (seen) confirmed++ else failed.add(rel.ifEmpty { "." } + (if (shellOut.isNotEmpty()) "  (shell: $shellOut)" else ""))
    }
    val probePass = failed.isEmpty() && dirs.isNotEmpty()
    println("${if (probePass) "PASS" else "FAIL"}  live probe through ${base.probeRoot}: $confirmed/${dirs.size} watched dirs reported the write")
    for (f in failed) println("      NOT SEEN: $f")
    if (!probePass) ok = false

    // 3. what is on disk but deliberately not watched
    val skipped = File(base.root).walkTopDown().filter { it.isDirectory }.map { it.absolutePath }
        .filter { it != base.root && it !in dirs }.toList()
    println("INFO  directories under root that are NOT watched: ${skipped.size}")
    for (s in skipped.take(15)) println("      ${monitor.relOf(s)}")
    if (skipped.size > 15) println("      ... and ${skipped.size - 15} more")

    monitor.stop()
    println(if (ok) "=== ALL DIRECTORY CHECKS PASSED ===" else "=== DIRECTORY CHECKS FAILED ===")
    return ok
}

// ---------------------------------------------------------------------------
// Entry Point
// ---------------------------------------------------------------------------
fun main(args: Array<String>) {
    val cfg = parseArgs(args)

    if (cfg.checkDirs) {
        exitProcess(if (runDirCheck(cfg)) 0 else 1)
    }

    if (cfg.checkEnv) {
        val ok = runEnvironmentChecks()
        exitProcess(if (ok) 0 else 1)
    }
    if (cfg.selfTest) {
        val ok = runEndToEndSelfTest()
        exitProcess(if (ok) 0 else 1)
    }

    val monitor = RadarMonitor(cfg)
    Runtime.getRuntime().addShutdownHook(Thread {
        monitor.stop()
    })
    monitor.start()
}