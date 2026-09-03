// Corpus-specific, parameter-free variant of run_export.sc: joern.bat passes
// --param values through cmd.exe, which eats `|` in regexes. For anything
// beyond the corpus use run_export.sc and quote/escape for your shell.
//
//   joern.bat --script bench/joern/corpus_flows.sc
import io.shiftleft.semanticcpg.language._
import io.joern.dataflowengineoss.language._
import io.joern.dataflowengineoss.queryengine.EngineContext

@main def exec(repo: String = "C:/projects/graphifyrebuild/corpus/vuln_app",
               out: String = "C:/projects/graphifyrebuild/bench/joern/corpus-flows.json"): Unit = {
  importCode(repo, "vuln_app")
  implicit val ctx: EngineContext = EngineContext()
  val root = repo.replace('\\', '/').stripSuffix("/")
  def rel(f: String): String = {
    val n = f.replace('\\', '/')
    if (n.startsWith(root)) n.stripPrefix(root).stripPrefix("/") else n
  }
  val sources = "read_query_param|read_request_body|read_header"
  val sinks = "run_sql|run_shell|render_html"
  val src = cpg.call.name(sources)
  val snk = cpg.call.name(sinks).argument
  val flows = snk.reachableByFlows(src).l.map { path =>
    val elems = path.elements.map { e =>
      ujson.Obj(
        "file"   -> rel(e.location.filename),
        "line"   -> e.lineNumber.map(_.intValue).getOrElse(-1),
        "method" -> e.location.methodFullName,
        "code"   -> e.code.take(200))
    }
    ujson.Obj("rule" -> "corpus-taint", "path" -> ujson.Arr(elems: _*))
  }
  os.write.over(os.Path(out, os.pwd), ujson.write(ujson.Obj("flows" -> ujson.Arr(flows: _*)), indent = 2))
  println(s"wrote ${flows.size} flow(s) to $out")
}
