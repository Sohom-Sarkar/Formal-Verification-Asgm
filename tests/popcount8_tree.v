// Revision: balanced adder tree. Same count, logarithmic depth instead of
// linear, and no shared structure with the sequential accumulator.
module popcount8(input [7:0] a, output [3:0] y);
  wire [1:0] p0, p1, p2, p3;
  wire [2:0] q0, q1;

  assign p0 = {1'b0, a[0]} + {1'b0, a[1]};
  assign p1 = {1'b0, a[2]} + {1'b0, a[3]};
  assign p2 = {1'b0, a[4]} + {1'b0, a[5]};
  assign p3 = {1'b0, a[6]} + {1'b0, a[7]};

  assign q0 = {1'b0, p0} + {1'b0, p1};
  assign q1 = {1'b0, p2} + {1'b0, p3};

  assign y  = {1'b0, q0} + {1'b0, q1};
endmodule
