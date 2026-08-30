// Revision: carry-save (Wallace-style) array multiplier.
//
// Instead of rippling a carry through a full adder at every stage, each stage
// is a bitwise 3:2 compressor that keeps the running total in redundant
// carry-save form (s, cy) with the invariant
//
//     s + cy  ==  sum of partial products absorbed so far
//
// using the per-bit identity  x + y + z == (x^y^z) + 2*majority(x,y,z).
// Only the final stage pays for a carry-propagating addition. This shares no
// structure with the reference, so the equivalence is a real proof obligation
// rather than something structural hashing can collapse.
module mult #(parameter WIDTH = 8)
             (input [WIDTH-1:0] a, input [WIDTH-1:0] b,
              output [2*WIDTH-1:0] p);

  localparam LANE = 2*WIDTH + 4;        // padded so nothing truncates
  localparam TOP  = LANE*(WIDTH-1);     // offset of the final stage

  wire [LANE*WIDTH-1:0] s;
  wire [LANE*WIDTH-1:0] cy;

  // Stage 0: the first partial product, with nothing carried in.
  assign s[LANE-1:0]  = {{(LANE-WIDTH){1'b0}}, a} & {LANE{b[0]}};
  assign cy[LANE-1:0] = {LANE{1'b0}};

  genvar i;
  generate
    for (i = 1; i < WIDTH; i = i + 1) begin : csa
      wire [LANE-1:0] pp;
      wire [LANE-1:0] ps;
      wire [LANE-1:0] pc;

      assign pp = ({{(LANE-WIDTH){1'b0}}, a} & {LANE{b[i]}}) << i;
      assign ps = s[LANE*(i-1)+LANE-1 : LANE*(i-1)];
      assign pc = cy[LANE*(i-1)+LANE-1 : LANE*(i-1)];

      assign s[LANE*i+LANE-1 : LANE*i]  = ps ^ pc ^ pp;
      assign cy[LANE*i+LANE-1 : LANE*i] =
          ((ps & pc) | (ps & pp) | (pc & pp)) << 1;
    end
  endgenerate

  // One carry-propagating add collapses the redundant form.
  assign p = s[TOP+LANE-1 : TOP] + cy[TOP+LANE-1 : TOP];
endmodule
