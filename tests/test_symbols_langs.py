"""Extent, qualification and call collection for the non-Python/JS grammars.

Every grammar here is a hard dependency of graphify itself, so a graph built
by graphify always has the parser its own nodes came from. Each fixture pins:
the definitions found (qualified), their kinds, that ``def_line`` lands on the
declaration, that the extent covers the whole construct, and the callee leaf
names collected from the body -- the four things `context`, `supplement` and
the gap disclosure rely on.
"""

from __future__ import annotations

import pytest

from graphify_ext import symbols

GO = b'''package p

type Store struct {
\titems map[string]int
}

func (s *Store) Get(k string) int {
\treturn normalise(s.items[k]) + helper.Lookup(k)
}

func normalise(v int) int {
\tif v < 0 {
\t\treturn 0
\t}
\treturn v
}
'''

JAVA = b'''package p;

public class Account {
    private int balance;

    public Account(int b) { this.balance = b; }

    public int withdraw(int amount) {
        validate(amount);
        return Ledger.record(this.balance - amount);
    }

    private static void validate(int a) {
        if (a < 0) throw new IllegalArgumentException();
    }
}

interface Ledger { int record(int v); }
'''

RUST = b'''pub struct Parser {
    pos: usize,
}

impl Parser {
    pub fn new() -> Self {
        Parser { pos: 0 }
    }

    pub fn parse(&mut self, s: &str) -> usize {
        let n = count_tokens(s);
        self.advance(n);
        util::finish(n)
    }
}

fn count_tokens(s: &str) -> usize {
    s.len()
}

trait Visitor {
    fn visit(&self);
}

enum Mode { Fast, Slow }
'''

RUBY = b'''module Billing
  class Invoice
    def total(items)
      subtotal = sum_items(items)
      Tax.apply(subtotal)
    end

    def self.build(attrs)
      new(attrs)
    end
  end
end

def sum_items(items)
  items.sum
end
'''

PHP = b'''<?php
namespace App;

class Cart {
    public function total(array $items): int {
        $sum = array_sum($items);
        return $this->applyTax(Tax::rate($sum));
    }

    private function applyTax(int $v): int { return $v; }
}

function helper($x) { return $x; }

interface Payable { public function pay(); }
'''

KOTLIN = b'''package p

class Greeter(val name: String) {
    fun greet(): String {
        val msg = format(name)
        return Logger.log(msg)
    }
}

fun format(s: String): String = s.trim()

object Registry {
    fun register(g: Greeter) {}
}
'''

CSHARP = b'''namespace App {
    public class Order {
        public int Total(int[] items) {
            var sum = Sum(items);
            return Tax.Apply(this.Round(sum));
        }

        private static int Sum(int[] xs) => xs.Length;

        public Order() { }
    }

    public interface IShippable { void Ship(); }
}
'''


def _names(src: bytes, path: str) -> dict:
    defs = symbols.definitions_from_source(src, path)
    assert defs is not None, "grammar must parse the fixture"
    return {d.name: d for d in defs}


def test_go_functions_methods_and_types():
    d = _names(GO, "store.go")
    assert set(d) == {"Store", "Get", "normalise"}
    assert d["Store"].kind == "class" and d["Get"].kind == "function"
    assert d["Get"].def_line == 7 and (d["Get"].start, d["Get"].end) == (7, 9)
    assert d["Get"].signature.startswith("func (s *Store) Get(k string) int")
    assert set(d["Get"].calls) == {"normalise", "Lookup"}


def test_java_classes_methods_constructors_and_interfaces():
    d = _names(JAVA, "Account.java")
    assert set(d) == {"Account", "Account.Account", "Account.withdraw",
                      "Account.validate", "Ledger", "Ledger.record"}
    assert d["Account"].kind == "class" and d["Ledger"].kind == "class"
    assert d["Account.withdraw"].def_line == 8
    assert (d["Account.withdraw"].start, d["Account.withdraw"].end) == (8, 11)
    assert set(d["Account.withdraw"].calls) == {"validate", "record"}
    # `new IllegalArgumentException()` is a constructor call, collected too
    assert "IllegalArgumentException" in d["Account.validate"].calls


def test_rust_impl_methods_are_qualified_by_their_type():
    d = _names(RUST, "parser.rs")
    assert set(d) == {"Parser", "Parser.new", "Parser.parse", "count_tokens",
                      "Visitor", "Visitor.visit", "Mode"}
    assert d["Parser.parse"].def_line == 10
    assert (d["Parser.parse"].start, d["Parser.parse"].end) == (10, 14)
    assert set(d["Parser.parse"].calls) == {"count_tokens", "advance", "finish"}
    assert d["Visitor"].kind == "class" and d["Mode"].kind == "class"


def test_ruby_modules_classes_methods_and_singletons():
    d = _names(RUBY, "invoice.rb")
    assert set(d) == {"Billing", "Billing.Invoice", "Billing.Invoice.total",
                      "Billing.Invoice.build", "sum_items"}
    t = d["Billing.Invoice.total"]
    assert t.def_line == 3 and (t.start, t.end) == (3, 6)
    assert set(t.calls) == {"sum_items", "apply"}
    assert d["Billing.Invoice.build"].kind == "function"
    assert d["Billing"].kind == "class"


def test_php_classes_methods_functions_and_interfaces():
    d = _names(PHP, "Cart.php")
    assert set(d) == {"Cart", "Cart.total", "Cart.applyTax", "helper",
                      "Payable", "Payable.pay"}
    t = d["Cart.total"]
    assert t.def_line == 5 and (t.start, t.end) == (5, 8)
    assert set(t.calls) == {"array_sum", "applyTax", "rate"}


def test_kotlin_classes_objects_and_functions():
    d = _names(KOTLIN, "Greeter.kt")
    assert set(d) == {"Greeter", "Greeter.greet", "format", "Registry",
                      "Registry.register"}
    g = d["Greeter.greet"]
    assert g.def_line == 4 and (g.start, g.end) == (4, 7)
    assert set(g.calls) == {"format", "log"}
    assert d["Registry"].kind == "class"


def test_csharp_classes_methods_constructors_and_interfaces():
    d = _names(CSHARP, "Order.cs")
    assert set(d) == {"Order", "Order.Total", "Order.Sum", "Order.Order",
                      "IShippable", "IShippable.Ship"}
    t = d["Order.Total"]
    assert t.def_line == 3 and (t.start, t.end) == (3, 6)
    assert set(t.calls) == {"Sum", "Apply", "Round"}


@pytest.mark.parametrize("src,path,line,leaf", [
    (GO, "store.go", 11, "normalise"),
    (JAVA, "Account.java", 13, "validate"),
    (RUST, "parser.rs", 17, "count_tokens"),
    (RUBY, "invoice.rb", 14, "sum_items"),
    (PHP, "Cart.php", 13, "helper"),
    (KOTLIN, "Greeter.kt", 10, "format"),
    (CSHARP, "Order.cs", 8, "Sum"),
])
def test_resolve_by_def_line_returns_the_leaf_name(tmp_path, src, path, line, leaf):
    (tmp_path / path).write_bytes(src)
    sym = symbols.resolve(tmp_path, path, line, expect=f"{leaf}()")
    assert sym is not None and sym.name == leaf
    assert sym.def_line == line
    assert sym.source.splitlines()[0].strip() != ""


def test_unknown_extension_is_still_refused_not_guessed(tmp_path):
    (tmp_path / "x.zig").write_text("fn main() void {}\n", encoding="utf-8")
    got = symbols.resolve_detail(tmp_path, "x.zig", 1)
    assert isinstance(got, symbols.Unresolved)
    assert got.code == symbols.UNSUPPORTED_LANGUAGE
