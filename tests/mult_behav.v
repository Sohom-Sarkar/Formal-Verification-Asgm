// Reference: unsigned NxN multiplier, described behaviourally.
// WIDTH is overridable from the command line with -p WIDTH=<n>.
module mult #(parameter WIDTH = 8)
             (input [WIDTH-1:0] a, input [WIDTH-1:0] b,
              output [2*WIDTH-1:0] p);
  assign p = a * b;
endmodule
